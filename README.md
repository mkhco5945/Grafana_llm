# Local Grafana + Prometheus playground

This project contains a small monitoring stack with no dashboards or AI integrations yet:

- Grafana on <http://localhost:3000>
- Prometheus on <http://localhost:9090>
- Demo metrics application on <http://localhost:8000>

Start it with `docker compose up -d` and stop it with `docker compose down`. Named volumes preserve Grafana and Prometheus data across container restarts.
