import { createSign } from "node:crypto";
import { readFile } from "node:fs/promises";

const DEFAULT_SCOPE = "https://www.googleapis.com/auth/spreadsheets";

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

async function getAccessToken(scope = DEFAULT_SCOPE) {
  const credentialsPath =
    process.env.GOOGLE_APPLICATION_CREDENTIALS ??
    ".secrets/google-service-account.json";
  const credentials = JSON.parse(await readFile(credentialsPath, "utf8"));
  const now = Math.floor(Date.now() / 1000);
  const unsignedJwt = [
    base64url({ alg: "RS256", typ: "JWT" }),
    base64url({
      iss: credentials.client_email,
      scope,
      aud: "https://oauth2.googleapis.com/token",
      exp: now + 3600,
      iat: now,
    }),
  ].join(".");

  const signature = createSign("RSA-SHA256")
    .update(unsignedJwt)
    .sign(credentials.private_key, "base64url");

  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: `${unsignedJwt}.${signature}`,
    }),
  });

  if (!response.ok) {
    throw new Error(`Google OAuth failed: ${response.status} ${await response.text()}`);
  }

  const payload = await response.json();
  return payload.access_token;
}

export function quoteSheetName(sheetName) {
  return `'${sheetName.replaceAll("'", "''")}'`;
}

export function columnLetter(index) {
  let value = index + 1;
  let output = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    output = String.fromCharCode(65 + remainder) + output;
    value = Math.floor((value - 1) / 26);
  }
  return output;
}

const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Single Sheets API call, with retry on transient throttling.
 *
 * The Sheets API enforces a per-user READ quota of 60 requests/minute. A run
 * that writes and then independently audits easily exceeds that: the writer
 * reads every target row, then the audit re-reads every tab. On 2026-08-05 a
 * GitHub Actions run wrote successfully and then FAILED in the audit step with
 * 429 RESOURCE_EXHAUSTED -- the data was fine, the verification could not
 * complete. A verification step that dies on throttling reports a false
 * failure, which is just as damaging as a false success.
 *
 * So 429/500/503 are retried with exponential backoff. Nothing else is: a real
 * 403 or 404 must still fail loudly and fast.
 */
export async function sheetsRequest(path, options = {}) {
  const maxAttempts = options.maxAttempts ?? 6;
  let attempt = 0;

  for (;;) {
    attempt += 1;
    const token = await getAccessToken(options.scope);
    const response = await fetch(`https://sheets.googleapis.com/v4${path}`, {
      method: options.method ?? "GET",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        ...(options.headers ?? {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });

    const text = await response.text();
    if (response.ok) return text ? JSON.parse(text) : {};

    const transient = [429, 500, 503].includes(response.status);
    if (!transient || attempt >= maxAttempts) {
      throw new Error(`Google Sheets request failed: ${response.status} ${text}`);
    }

    // Quota windows are per-minute, so back off in real seconds, not millis.
    const waitMs = Math.min(60000, 5000 * 2 ** (attempt - 1));
    console.warn(
      `[sheets] ${response.status} throttled; retry ${attempt}/${maxAttempts - 1} in ${Math.round(waitMs / 1000)}s`,
    );
    await sleepMs(waitMs);
  }
}

export async function getValues(spreadsheetId, range, options = {}) {
  const params = new URLSearchParams({
    valueRenderOption: options.valueRenderOption ?? "FORMATTED_VALUE",
    dateTimeRenderOption: options.dateTimeRenderOption ?? "FORMATTED_STRING",
  });
  const path = `/spreadsheets/${spreadsheetId}/values/${encodeURIComponent(range)}?${params}`;
  const response = await sheetsRequest(path, { scope: options.scope });
  return response.values ?? [];
}

/**
 * Write values in paced chunks with retry.
 *
 * Why this is not one big request: the Sheets API rejects bursts from a
 * service account with BOTH 429 (quota) and, under sustained pressure, a
 * misleading 403 PERMISSION_DENIED -- on cells the same account can
 * demonstrably write a minute later. A single 60-cell batch is atomic, so one
 * throttled call loses the entire backfill and looks like a permissions bug.
 *
 * Chunking + pacing + retry converts a transient throttle into a short delay
 * instead of a total, misdiagnosed failure. 403 is retried ONLY when a prior
 * chunk in the same run already succeeded, which proves access is real.
 */
export async function batchUpdateValues(spreadsheetId, data, options = {}) {
  const path = `/spreadsheets/${spreadsheetId}/values:batchUpdate`;
  const chunkSize = options.chunkSize ?? 10;
  const pauseMs = options.pauseMs ?? 1200;
  const maxAttempts = options.maxAttempts ?? 5;

  const send = (chunk) =>
    sheetsRequest(path, {
      method: "POST",
      scope: options.scope,
      body: {
        valueInputOption: options.valueInputOption ?? "USER_ENTERED",
        data: chunk,
      },
    });

  if (data.length <= chunkSize && !options.forceChunk) {
    return send(data);
  }

  // Group updates by target tab. Empirically, a single batch that spans
  // multiple sheets is rejected 403 PERMISSION_DENIED, while the exact same
  // cells written one-tab-at-a-time succeed. Per-tab batching is the shape
  // this spreadsheet actually accepts.
  const byTab = new Map();
  for (const item of data) {
    const tab = String(item.range).split("!")[0];
    if (!byTab.has(tab)) byTab.set(tab, []);
    byTab.get(tab).push(item);
  }
  const chunks = [];
  for (const items of byTab.values()) {
    for (let i = 0; i < items.length; i += chunkSize) {
      chunks.push(items.slice(i, i + chunkSize));
    }
  }

  let totalUpdatedCells = 0;
  let anySucceeded = false;

  for (let c = 0; c < chunks.length; c += 1) {
    const chunk = chunks[c];
    let attempt = 0;
    for (;;) {
      attempt += 1;
      try {
        const res = await send(chunk);
        totalUpdatedCells += res.totalUpdatedCells ?? 0;
        anySucceeded = true;
        break;
      } catch (error) {
        const msg = String(error.message || "");
        const throttled =
          msg.includes(" 429") ||
          msg.includes(" 500") ||
          msg.includes(" 503") ||
          (msg.includes(" 403") && anySucceeded);
        if (!throttled || attempt >= maxAttempts) {
          const ranges = chunk.map((d) => d.range).join(", ");
          throw new Error(`${msg}\n  failing ranges: ${ranges}`);
        }
        await sleepMs(Math.min(30000, 3000 * 2 ** (attempt - 1)));
      }
    }
    // Pace between chunks, but not after the last one.
    // BUG FIXED 2026-08-05: this line read `if (i + chunkSize < data.length)`,
    // but `i` belongs to the chunk-building loop above, which had already
    // closed -- so this threw `ReferenceError: i is not defined` on ANY write
    // large enough to require more than one chunk. It went unnoticed because
    // every recent run was either a single chunk or an idempotent no-op. The
    // first real multi-chunk week would have crashed mid-write.
    if (c < chunks.length - 1) await sleepMs(pauseMs);
  }

  return { totalUpdatedCells };
}
