# Start with a lightweight Python 3.11 image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install ffmpeg for video transcoding
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install the Python libraries (we use standard install for this first test)
RUN pip install --no-cache-dir -r requirements.txt

# Copy the actual application code into the container
COPY ./app ./app

# Expose the port that Uvicorn (FastAPI) runs on
EXPOSE 8000

# Start the web server with live reloading enabled
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]