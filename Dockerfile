# Use Alpine Linux as the base image
FROM python:3.11-alpine

# Install system dependencies
RUN apk add --no-cache \
    fuse \
    fuse-dev \
    gcc \
    musl-dev \
    python3-dev

# Set up FUSE configuration
RUN echo "user_allow_other" >> /etc/fuse.conf

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 6000 available to the world outside this container
EXPOSE 6000

# Run main.py when the container launches
CMD ["python", "main.py", "/mnt/translated", "/mnt/original", "database/translations.db", "backups"]
