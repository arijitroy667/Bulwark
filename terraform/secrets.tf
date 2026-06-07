# ============================================================
# Bulwark — Secret Manager Resources
# ============================================================
# All 7+ secrets required by Bulwark, stored securely in
# Google Cloud Secret Manager.
# ============================================================

# ── GitLab Webhook Token ──
resource "google_secret_manager_secret" "gitlab_webhook_token" {
  secret_id = "GITLAB_WEBHOOK_TOKEN"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "gitlab_webhook_token" {
  secret      = google_secret_manager_secret.gitlab_webhook_token.id
  secret_data = var.gitlab_webhook_token
}

# ── GitLab PAT ──
resource "google_secret_manager_secret" "gitlab_pat" {
  secret_id = "GITLAB_PAT"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "gitlab_pat" {
  secret      = google_secret_manager_secret.gitlab_pat.id
  secret_data = var.gitlab_pat
}

# ── Dynatrace API Token ──
resource "google_secret_manager_secret" "dynatrace_api_token" {
  secret_id = "DYNATRACE_API_TOKEN"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "dynatrace_api_token" {
  secret      = google_secret_manager_secret.dynatrace_api_token.id
  secret_data = var.dynatrace_api_token
}

# ── Dynatrace Environment ID ──
resource "google_secret_manager_secret" "dynatrace_env_id" {
  secret_id = "DYNATRACE_ENV_ID"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "dynatrace_env_id" {
  secret      = google_secret_manager_secret.dynatrace_env_id.id
  secret_data = var.dynatrace_env_id
}

# ── Arize API Key ──
resource "google_secret_manager_secret" "arize_api_key" {
  secret_id = "ARIZE_API_KEY"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "arize_api_key" {
  secret      = google_secret_manager_secret.arize_api_key.id
  secret_data = var.arize_api_key
}

# ── Arize Space Key ──
resource "google_secret_manager_secret" "arize_space_key" {
  secret_id = "ARIZE_SPACE_KEY"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "arize_space_key" {
  secret      = google_secret_manager_secret.arize_space_key.id
  secret_data = var.arize_space_key
}

# ── MongoDB URI ──
resource "google_secret_manager_secret" "mongodb_uri" {
  secret_id = "MONGODB_URI"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "mongodb_uri" {
  secret      = google_secret_manager_secret.mongodb_uri.id
  secret_data = var.mongodb_uri
}

# ── Gemini API Key ──
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "GEMINI_API_KEY"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "gemini_api_key" {
  secret      = google_secret_manager_secret.gemini_api_key.id
  secret_data = var.gemini_api_key
}
