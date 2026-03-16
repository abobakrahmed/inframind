terraform {
  backend "gcs" {
    bucket = "terraform-tfstat"
    prefix = "terraform.tfstate"
  credentials = "/app/gcp-credentials.json"
  }
}
