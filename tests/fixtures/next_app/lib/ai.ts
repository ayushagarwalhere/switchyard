import OpenAI from "openai";

// Shared client for the app's server-side LLM calls.
const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const TRIAGE_SYSTEM = "You are a support triage assistant. Reply with one label: billing, bug, feature, other.";

export async function triageTicket(ticket: string): Promise<string> {
  const res = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    temperature: 0,
    messages: [
      { role: "system", content: TRIAGE_SYSTEM },
      { role: "user", content: `Ticket:\n${ticket}` },
    ],
  });
  return res.choices[0].message.content?.trim() ?? "other";
}

export const extractInvoice = async (doc: string) => {
  const res = await openai.chat.completions.create({
    model: "gpt-4o",
    response_format: { type: "json_object" },
    messages: [{ role: "user", content: "Extract vendor, total and due_date as JSON from this invoice:\n" + doc }],
  });
  return JSON.parse(res.choices[0].message.content ?? "{}");
};
