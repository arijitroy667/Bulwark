# ============================================================
# Bulwark — Terraform Outputs
# ============================================================

output "webhook_url" {
  description = "Public URL of the Bulwark webhook receiver"
  value       = google_cloud_run_v2_service.webhook_receiver.uri
}

output "webhook_endpoint" {
  description = "Full webhook endpoint to configure in GitLab"
  value       = "${google_cloud_run_v2_service.webhook_receiver.uri}/webhook/gitlab"
}

output "orchestrator_worker_url" {
  description = "URL of the Bulwark orchestrator worker"
  value       = google_cloud_run_v2_service.orchestrator_worker.uri
}

output "orchestrator_pubsub_endpoint" {
  description = "Endpoint for Orchestrator Pub/Sub pushes"
  value       = "${google_cloud_run_v2_service.orchestrator_worker.uri}/pubsub/orchestrate"
}

output "pubsub_topic" {
  description = "Pub/Sub topic for MR events"
  value       = google_pubsub_topic.mr_events.name
}

output "pubsub_subscription" {
  description = "Pub/Sub subscription for the orchestrator"
  value       = google_pubsub_subscription.orchestrator_sub.name
}

output "artifact_registry" {
  description = "Artifact Registry repository for simulation images"
  value       = google_artifact_registry_repository.bulwark_images.name
}

output "service_account_email" {
  description = "Service account email for the Bulwark orchestrator"
  value       = google_service_account.bulwark_orchestrator.email
}
