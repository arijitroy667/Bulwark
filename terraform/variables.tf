# ============================================================
# Bulwark — Terraform Variables
# ============================================================

variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
  default     = "bulwark-project"
}

variable "region" {
  description = "Google Cloud region"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment (development, staging, production)"
  type        = string
  default     = "development"
}

variable "mongodb_db_name" {
  description = "MongoDB database name"
  type        = string
  default     = "bulwark"
}

variable "gemini_model" {
  description = "Gemini model ID"
  type        = string
  default     = "gemini-2.0-flash"
}

# ── Secret values (passed via terraform.tfvars or environment) ──

variable "gitlab_webhook_token" {
  description = "GitLab webhook validation token"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gitlab_pat" {
  description = "GitLab Personal Access Token"
  type        = string
  sensitive   = true
  default     = ""
}

variable "dynatrace_api_token" {
  description = "Dynatrace API Token"
  type        = string
  sensitive   = true
  default     = ""
}

variable "dynatrace_env_id" {
  description = "Dynatrace Environment ID"
  type        = string
  sensitive   = true
  default     = ""
}

variable "arize_api_key" {
  description = "Arize API Key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "arize_space_key" {
  description = "Arize Space Key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "mongodb_uri" {
  description = "MongoDB Atlas connection URI"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gemini_api_key" {
  description = "Gemini API Key"
  type        = string
  sensitive   = true
  default     = ""
}
