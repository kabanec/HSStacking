# HS→PGA Lookup API

## Run Locally
1. `git clone <your-repo-url>`
2. `cd PGA_flags`
3. Create `.env` with your OpenAI API key
4. `docker build -t hs-pga . && docker run -p 8000:8000 --env-file .env hs-pga`

## Deploy on IONOS Cloud
1. Create Docker Container via IONOS Cloud Console
2. Point image registry at GitHub Container Registry
3. Set ENV vars in IONOS UI
4. Expose port 8000 and start