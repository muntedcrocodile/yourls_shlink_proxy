# Use the official Python 3.9 image
FROM python:3.9-slim

# Create a working directory
WORKDIR /app

# Copy the requirements.txt file to the container
COPY requirements.txt .

# Install the required packages
RUN pip install -r requirements.txt

# Copy the rest of the app to the container
COPY . .

# Set environment variables
# ENV SHLINK_URL=
# ENV SHLINK_API_KEY=

# Set the WSGI server to use Gunicorn
CMD ["gunicorn", "-w", "4", "-b", ":8080", "--log-file", "-", "app:app"]
