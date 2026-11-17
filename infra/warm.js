/**
 * Keep-warm pinger for graphwright.
 *
 * One request to /warm does both jobs:
 *   - inbound traffic resets Render's 15-minute idle timer
 *   - the handler runs `RETURN 1`, resetting AuraDB's 72-hour inactivity timer
 *
 * Runs on a Cloudflare Worker cron (free plan, no card). Only pings inside a local
 * time window so the Render free tier's 750 instance-hours/month aren't exhausted:
 * 15 h/day ≈ 450 h/month, versus 730 h if it ran around the clock.
 */

const DEFAULT_START_HOUR = 8; // local time
const DEFAULT_END_HOUR = 23; // exclusive

function localHour(tzOffsetHours) {
  const now = new Date();
  return (now.getUTCHours() + tzOffsetHours + 24) % 24;
}

export default {
  async scheduled(_event, env, ctx) {
    const offset = Number(env.TZ_OFFSET_HOURS ?? 0);
    const start = Number(env.START_HOUR ?? DEFAULT_START_HOUR);
    const end = Number(env.END_HOUR ?? DEFAULT_END_HOUR);
    const hour = localHour(offset);

    if (hour < start || hour >= end) {
      console.log(`skip: local hour ${hour} outside ${start}-${end}`);
      return;
    }

    const url = `${env.WARM_URL.replace(/\/$/, '')}/warm`;
    const started = Date.now();
    try {
      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${env.MCP_AUTH_TOKEN}` },
      });
      const body = await response.text();
      console.log(`warm ${response.status} ${Date.now() - started}ms ${body.slice(0, 120)}`);
    } catch (error) {
      // A timeout here is often just a Render cold start; the wake still happens.
      console.error(`warm failed after ${Date.now() - started}ms: ${error}`);
    }
  },

  // Manual trigger for testing: https://<worker>.<subdomain>.workers.dev/
  async fetch(_request, env) {
    const response = await fetch(`${env.WARM_URL.replace(/\/$/, '')}/warm`, {
      headers: { Authorization: `Bearer ${env.MCP_AUTH_TOKEN}` },
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { 'content-type': 'application/json' },
    });
  },
};
