# Submission logistics

- `docker compose up` starts your whole system: your service, Postgres and the `payswift` service already defined in `docker-compose.yml`. Keep the `payswift` service as it is.
- Your service listens on port **8000** and answers `GET /health` with 200 when it is ready.
- Your service reads the PaySwift URL from the environment variable `PAYSWIFT_BASE_URL`.
- List every environment variable your service needs in `.env.example`. Don't commit `.env` or any key.
- We use whatever is on `main` of the repo you link when your 24 hours end.
