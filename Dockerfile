FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy only requirements first for caching
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Now copy the rest of the code (changes frequently)
COPY . ./

# Runs as non-root - /app/mountpoint is a host bind mount (see
# docker-compose.yml), chowned to this same UID on the host side so
# writes (guestbook.db) still work.
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Set the default command
CMD ["python", "./main.py"]
