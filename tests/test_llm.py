import asyncio
import os
from dotenv import load_dotenv
from litellm import acompletion

load_dotenv()

async def test_llm():
    model = os.getenv("META_MODEL_NAME", "vertex_ai/gemini-3.5-flash")
    project = os.getenv("VERTEX_PROJECT")
    location = os.getenv("VERTEX_LOCATION")
    
    print(f"Testing LLM: {model} in {project}/{location}")
    try:
        res = await acompletion(
            model=model,
            messages=[{"role": "user", "content": "Hello!"}],
            vertex_project=project,
            vertex_location=location
        )
        print("Success:", res.choices[0].message.content)
    except Exception as e:
        print("Error:", e)

asyncio.run(test_llm())
