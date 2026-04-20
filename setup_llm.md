```python
from openai import OpenAI
api_key = "sk-AyZC2ICNqvww5qJMn4targ"
base_url = "https://proxy.onebot.meobeo.ai/v1"
model_name = "hosted_vllm/Qwen/Qwen3.5-35B-A3B-FP8"
client = OpenAI(api_key=api_key, base_url=base_url)
response = client.chat.completions.create(
    model=model_name,
    messages=[{"role": "user", "content": "chào em"}],
    extra_body={
        "cache": {"no-cache": True},
        "chat_template_kwargs": {"enable_thinking": False},
    },
    # reasoning_effort="none"
)
result = response.choices[0].message.content
```