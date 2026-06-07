# ============================================================
# Bulwark — Main Terraform Configuration
# ============================================================
# Provisions all GCP infrastructure for Phase 1:
#   - Cloud Run service (webhook receiver)
#   - Pub/Sub topic + subscription
#   - Artifact Registry
#   - IAM service account
# ============================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ──────────────────────────────────────────────
# Enable required APIs
# ──────────────────────────────────────────────

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
  ])

  project = var.project_id
  service = each.key

  disable_on_destroy = false
}

# ──────────────────────────────────────────────
# IAM Service Account
# ──────────────────────────────────────────────

resource "google_service_account" "bulwark_orchestrator" {
  account_id   = "bulwark-orchestrator"
  display_name = "Bulwark Orchestrator Service Account"
  description  = "Service account for the Bulwark orchestrator with least-privilege permissions"
}

# Assign roles to the service account
resource "google_project_iam_member" "roles" {
  for_each = toset([
    "roles/run.invoker",
    "roles/run.admin",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/secretmanager.secretAccessor",
    "roles/artifactregistry.writer",
    "roles/cloudbuild.builds.editor",
    "roles/logging.logWriter",
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.bulwark_orchestrator.email}"
}

# ──────────────────────────────────────────────
# Artifact Registry
# ──────────────────────────────────────────────

resource "google_artifact_registry_repository" "bulwark_images" {
  location      = var.region
  repository_id = "bulwark-images"
  format        = "DOCKER"
  description   = "Docker images for Bulwark simulation environments"

  cleanup_policies {
    id     = "delete-old-images"
    action = "DELETE"
    condition {
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.apis["artifactregistry.googleapis.com"]]
}

# ──────────────────────────────────────────────
# Pub/Sub Topic + Subscription
# ──────────────────────────────────────────────

resource "google_pubsub_topic" "mr_events" {
  name = "bulwark-mr-events"

  message_retention_duration = "86400s" # 24 hours

  depends_on = [google_project_service.apis["pubsub.googleapis.com"]]
}

resource "google_pubsub_subscription" "orchestrator_sub" {
  name  = "bulwark-orchestrator-sub"
  topic = google_pubsub_topic.mr_events.id

  ack_deadline_seconds       = 600 # 10 minutes (simulations can take a while)
  message_retention_duration = "86400s"

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 3
  }

  depends_on = [google_project_service.apis["pubsub.googleapis.com"]]
}

resource "google_pubsub_topic" "dead_letter" {
  name = "bulwark-dead-letter"

  depends_on = [google_project_service.apis["pubsub.googleapis.com"]]
}

# ──────────────────────────────────────────────
# Cloud Run — Webhook Receiver
# ──────────────────────────────────────────────

resource "google_cloud_run_v2_service" "webhook_receiver" {
  name     = "bulwark-webhook-receiver"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.bulwark_orchestrator.email

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/bulwark-images/webhook-receiver:latest"

      ports {
        container_port = 8080
      }

      env {
        name  = "BULWARK_MODE"
        value = var.environment
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_REGION"
        value = var.region
      }

      # Secrets are injected via Secret Manager references
      env {
        name = "GITLAB_WEBHOOK_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gitlab_webhook_token.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GITLAB_PAT"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gitlab_pat.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "MONGODB_URI"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mongodb_uri.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 3
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        period_seconds    = 30
        failure_threshold = 3
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }
  }

  depends_on = [
    google_project_service.apis["run.googleapis.com"],
    google_artifact_registry_repository.bulwark_images,
  ]
}

# Make the webhook receiver publicly accessible (GitLab needs to reach it)
resource "google_cloud_run_v2_service_iam_member" "webhook_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.webhook_receiver.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
