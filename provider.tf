terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = "galvanized-opus-339521"
  region  = "us-central1"
  credentials = "/app/gcp-credentials.json"
}
