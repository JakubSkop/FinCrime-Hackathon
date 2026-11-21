/**
 * OpenRouter smoke request — one end-to-end call that prints response text.
 *
 * Default model slug: openai/gpt-4o-mini
 * (cheap, widely available text model on https://openrouter.ai/models)
 */

import { OpenRouter } from "@openrouter/sdk";
import type { OpenResponsesResult } from "@openrouter/sdk/models";

/** Cheap/reliable default for short verdict-style text. */
export const DEFAULT_MODEL = "openai/gpt-4o-mini";

const KEYS_URL = "https://openrouter.ai/keys";

function requireApiKey(): string {
  const apiKey = process.env.OPENROUTER_API_KEY?.trim();
  if (!apiKey) {
    console.error(
      "OPENROUTER_API_KEY is not set.\n" +
        `Create an API key at ${KEYS_URL} and export it, for example:\n` +
        `  export OPENROUTER_API_KEY=sk-or-...\n` +
        "Then re-run: npm run smoke",
    );
    process.exit(1);
  }
  return apiKey;
}

function extractText(response: OpenResponsesResult): string {
  if (response.outputText?.trim()) {
    return response.outputText.trim();
  }

  for (const item of response.output) {
    if (item.type !== "message" || !Array.isArray(item.content)) {
      continue;
    }
    for (const part of item.content) {
      if (part.type === "output_text" && typeof part.text === "string") {
        const text = part.text.trim();
        if (text) {
          return text;
        }
      }
    }
  }

  throw new Error("OpenRouter response completed but contained no output text");
}

async function main(): Promise<void> {
  const apiKey = requireApiKey();

  const client = new OpenRouter({
    apiKey,
    httpReferer: "https://github.com/JakubSkop/FinCrime-Hackathon",
    appTitle: "FinCrime-Hackathon OpenRouter smoke",
  });

  try {
    const response = await client.responses.send({
      responsesRequest: {
        model: DEFAULT_MODEL,
        temperature: 0,
        input:
          "Reply with exactly this phrase and nothing else: OpenRouter smoke OK",
      },
    });

    if (!("output" in response)) {
      throw new Error("Expected a non-streaming response");
    }
    if (response.status !== "completed") {
      throw new Error(
        response.error?.message ?? `Response ended as ${response.status}`,
      );
    }

    const text = extractText(response);
    console.log(text);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`OpenRouter request failed: ${message}`);
    process.exit(1);
  }
}

main();
