import json
from openai import OpenAI

client = OpenAI()
MODEL = "gpt-4o-mini"
SYSTEM = "You are a support triage assistant. Reply with one label: billing, bug, feature, other."


def triage(ticket: str) -> str:
    """Label a support ticket."""
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Ticket:\n{ticket}"}],
    )
    return resp.choices[0].message.content.strip()


def extract_invoice(text):
    prompt = "Extract vendor, total and due_date as JSON from this invoice:\n{doc}".format(doc=text)
    r = client.chat.completions.create(model="gpt-4o", response_format={"type": "json_object"},
                                       messages=[{"role": "user", "content": prompt}])
    return json.loads(r.choices[0].message.content)


def summarize(article, n=3):
    msgs = [{"role": "system", "content": "Summarize in " + str(n) + " bullet points."},
            {"role": "user", "content": article}]
    return client.chat.completions.create(model=pick_model(), messages=msgs).choices[0].message.content
