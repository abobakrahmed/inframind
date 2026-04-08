# Use a lightweight Python image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    unzip \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Terraform CLI
ARG TF_VERSION=1.7.5
RUN curl -LO https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip \
    && unzip terraform_${TF_VERSION}_linux_amd64.zip \
    && mv terraform /usr/local/bin/ \
    && rm terraform_${TF_VERSION}_linux_amd64.zip

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .

# Expose Streamlit port
EXPOSE 8080

# Run the application
CMD ["streamlit", "run", "ui.py", "--server.port=8080", "--server.address=0.0.0.0"]
