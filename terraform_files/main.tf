

# ── added ──────────────────────────────────────────────

# ── added ──────────────────────────────────────────────

# ── added ──────────────────────────────────────────────

# ── added ──────────────────────────────────────────────

# ── added ──────────────────────────────────────────────
data "google_compute_image" "debian_12" {
  family  = "debian-12"
  project = "debian-cloud"
}

# ── added ──────────────────────────────────────────────
resource "google_cloud_run_v2_service" "ai_sre_agent" {
  name     = "ai-sre-agent"
  location = "us-central1"

  template {
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
    }
  }
}
