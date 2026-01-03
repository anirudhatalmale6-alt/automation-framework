FROM python:3.11-slim

# Install system dependencies including fonts for proper rendering
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    curl \
    sqlite3 \
    # Fonts for proper text rendering
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-noto-cjk \
    fonts-freefont-ttf \
    # Additional dependencies for Chromium
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libgbm1 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and browser with all dependencies
RUN playwright install chromium --with-deps

# Copy application code
COPY src/ ./src/
COPY config/ ./config/

# Create data directories
RUN mkdir -p /app/data /app/data/browser /app/logs

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV BROWSER_HEADLESS=true
ENV BROWSER_DATA_DIR=/app/data/browser

# Health check endpoint
EXPOSE 8080

# Run the framework
CMD ["python", "-m", "main"]
