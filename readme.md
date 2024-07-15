# Yourls -> Flask -> Shlink, URL Shortener proxy.

This is a Flask application that translates a given URL into a shortened link using the Shlink API. The application comes with a Dockerfile and a docker-compose file to build and run the application using Docker.


You can build the image and run the application using the following commands:

```bash
docker build -t yourls_shlink_proxy .

docker run -p 8080:8080 yourls_shlink_proxy
```

Alternativly, you can build the image and run the application in a single step using the provided Docker Compose file:
```bash
docker-compose up -d --build
```

The application listens on port 8080 by default, and you can shorten a URL by sending a GET request to the root endpoint (e.g., http://localhost:8080?link=https://www.example.com). The response will be the the shortended link in raw text.


# Contributions

Contributions are welcome! Please open a pull request or an issue to discuss changes to the project.


# Donations

**XMR:**
`83NpmSwz69tAUYtMeUgN2rCRDfzFSsrRzAsw883dvMAU1qERF385ZtKMLTMdu19uqyN3vQQzDVcWJD1mRmnVwsckPZnYY7a`