"use server";
import { openai } from "@ai-sdk/openai";
import { generateText, streamText } from "ai";

export async function POST(req: Request) {
  const { article } = await req.json();
  const { text } = await generateText({
    model: openai("gpt-4o"),
    system: "Summarize the article in 3 bullet points.",
    prompt: article,
    maxOutputTokens: 300,
  });
  return Response.json({ summary: text });
}

export async function reply(question: string) {
  // the answer is streamed back to the browser
  const result = streamText({
    model: openai("gpt-4o-mini"),
    messages: [{ role: "user", content: `Answer briefly: ${question}` }],
  });
  return result.toTextStreamResponse();
}
