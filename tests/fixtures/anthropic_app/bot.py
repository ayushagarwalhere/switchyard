import anthropic

client = anthropic.Anthropic()


def classify(text: str) -> str:
    msg = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=50,
        system="Classify the sentiment as positive, negative or neutral. One word.",
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text.strip()
