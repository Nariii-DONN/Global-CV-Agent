import { Container } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

// Single stateful instance: the Python app keeps SQLite + uploads on local
// disk, so every request routes to the same container ("singleton").
export class AgentContainer extends Container {
	defaultPort = 8787;
	// Background searches run in threads after the POST returns; keep the
	// container awake long enough for long runs. Tune down to save usage.
	sleepAfter = "30m";
	envVars = {
		APP_HOST: "0.0.0.0",
		APP_PORT: "8787",
		DATABASE_PATH: "./data/cv_agent.db",
		UPLOAD_DIR: "./data/uploads",
		MAX_DOWNLOAD_MB: "8",
		MAX_HTML_MB: "3",
		MIN_MATCH_SCORE: "0",
		FREE_SEARCH_ENABLED: "1",
		FREE_SEARCH_DELAY: "2.0",
		MAX_QUERIES_PER_JD: "25",
		RESULTS_PER_QUERY: "10",
		MAX_CANDIDATES_PER_RUN: "60",
		FETCH_WORKERS: "6",
		SEARCH_COUNTRY: "IN",
		SEARCH_LANGUAGE: "en",
		LOG_LEVEL: "INFO",
		// Secrets below come from `wrangler secret put <NAME>` (never committed).
		ADMIN_NAME: env.ADMIN_NAME || "",
		ADMIN_EMAIL: env.ADMIN_EMAIL || "",
		ADMIN_PHONE: env.ADMIN_PHONE || "",
		ADMIN_PASSWORD_SHA256: env.ADMIN_PASSWORD_SHA256 || "",
		ADMIN_SECRET_SHA256: env.ADMIN_SECRET_SHA256 || "",
		SESSION_TIMEOUT_SEC: "43200",
	};
}

export default {
	async fetch(request, env) {
		const container = env.AGENT.getByName("singleton");
		return container.fetch(request);
	},
};
