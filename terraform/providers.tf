terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Points Terraform at the remote state file living in your GCP project —
  # not at a file on this PC.
  backend "gcs" {
    bucket = "cloud-resume-2026-tfstate"
    prefix = "cloud-resume"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}