# Use an official Python runtime as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app

# Install any needed packages specified in requirements.txt
# (You'll need to create this file with your project dependencies)
RUN pip install --no-cache-dir -r requirements.txt

# Make port 6000 available to the world outside this container
EXPOSE 6000

# Run main.py when the container launches
CMD ["python", "main.py", "/mnt/translated", "/mnt/original", "database/translations.db", "backups"]
