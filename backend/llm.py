import copy
from enum import Enum
from typing import Any, Awaitable, Callable, List, cast
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionChunk
from config import IS_DEBUG_ENABLED
from debug.DebugFileWriter import DebugFileWriter
from image_processing.utils import process_image

from utils import pprint_prompt


# Actual model versions that are passed to the LLMs and stored in our logs
class Llm(Enum):
    GPT_4_VISION = "gpt-4-vision-preview"
    GPT_4_TURBO_2024_04_09 = "gpt-4-turbo-2024-04-09"
    GPT_4O_2024_05_13 = "gpt-4o-2024-05-13"
    GPT_4O_2024_08_06 = "gpt-4o-2024-08-06"
    GPT_4O_2024_11_20 = "gpt-4o-2024-11-20"
    CLAUDE_3_SONNET = "claude-3-sonnet-20240229"
    CLAUDE_3_OPUS = "claude-3-opus-20240229"
    CLAUDE_3_HAIKU = "claude-3-haiku-20240307"
    CLAUDE_3_5_SONNET_2024_06_20 = "claude-3-5-sonnet-20240620"
    CLAUDE_3_5_SONNET_2024_10_22 = "claude-3-5-sonnet-20241022"
    GEMINI_2_0_FLASH_EXP = "gemini-2.0-flash-exp"
    O1_2024_12_17 = "o1-2024-12-17"


# Will throw errors if you send a garbage string
def convert_frontend_str_to_llm(frontend_str: str) -> Llm:
    if frontend_str == "gpt_4_vision":
        return Llm.GPT_4_VISION
    elif frontend_str == "claude_3_sonnet":
        return Llm.CLAUDE_3_SONNET
    else:
        return Llm(frontend_str)


async def stream_openai_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    base_url: str | None,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # Base parameters
    params = {
        "model": model.value,
        "messages": messages,
        "stream": True,
        "timeout": 600,
        "temperature": 0.0,
    }

    # Add 'max_tokens' only if the model is a GPT4 vision or Turbo model
    if (
        model == Llm.GPT_4_VISION
        or model == Llm.GPT_4_TURBO_2024_04_09
        or model == Llm.GPT_4O_2024_05_13
    ):
        params["max_tokens"] = 4096

    if model == Llm.GPT_4O_2024_11_20:
        params["max_tokens"] = 16384

    stream = await client.chat.completions.create(**params)  # type: ignore
    full_response = ""
    async for chunk in stream:  # type: ignore
        assert isinstance(chunk, ChatCompletionChunk)
        if (
            chunk.choices
            and len(chunk.choices) > 0
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ):
            content = chunk.choices[0].delta.content or ""
            full_response += content
            await callback(content)

    await client.close()

    return full_response


# TODO: Have a seperate function that translates OpenAI messages to Claude messages
async def stream_claude_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:

    client = AsyncAnthropic(api_key=api_key)

    # Base parameters
    max_tokens = 8192
    temperature = 0.0

    # Translate OpenAI messages to Claude messages

    # Deep copy messages to avoid modifying the original list
    cloned_messages = copy.deepcopy(messages)

    system_prompt = cast(str, cloned_messages[0].get("content"))
    claude_messages = [dict(message) for message in cloned_messages[1:]]
    for message in claude_messages:
        if not isinstance(message["content"], list):
            continue

        for content in message["content"]:  # type: ignore
            if content["type"] == "image_url":
                content["type"] = "image"

                # Extract base64 data and media type from data URL
                # Example base64 data URL: data:image/png;base64,iVBOR...
                image_data_url = cast(str, content["image_url"]["url"])

                # Process image and split media type and data
                # so it works with Claude (under 5mb in base64 encoding)
                (media_type, base64_data) = process_image(image_data_url)

                # Remove OpenAI parameter
                del content["image_url"]

                content["source"] = {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_data,
                }

    # Stream Claude response
    async with client.messages.stream(
        model=model.value,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=claude_messages,  # type: ignore
        extra_headers={"anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15"},
    ) as stream:
        async for text in stream.text_stream:
            await callback(text)

    # Return final message
    response = await stream.get_final_message()

    # Close the Anthropic client
    await client.close()

    return response.content[0].text


async def stream_claude_response_native(
    system_prompt: str,
    messages: list[Any],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    include_thinking: bool = False,
    model: Llm = Llm.CLAUDE_3_OPUS,
) -> str:

    client = AsyncAnthropic(api_key=api_key)

    # Base model parameters
    max_tokens = 4096
    temperature = 0.0

    # Multi-pass flow
    current_pass_num = 1
    max_passes = 2

    prefix = "<thinking>"
    response = None

    # For debugging
    full_stream = ""
    debug_file_writer = DebugFileWriter()

    while current_pass_num <= max_passes:
        current_pass_num += 1

        # Set up message depending on whether we have a <thinking> prefix
        messages_to_send = (
            messages + [{"role": "assistant", "content": prefix}]
            if include_thinking
            else messages
        )

        pprint_prompt(messages_to_send)

        async with client.messages.stream(
            model=model.value,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=messages_to_send,  # type: ignore
        ) as stream:
            async for text in stream.text_stream:
                print(text, end="", flush=True)
                full_stream += text
                await callback(text)

        response = await stream.get_final_message()
        response_text = response.content[0].text

        # Write each pass's code to .html file and thinking to .txt file
        if IS_DEBUG_ENABLED:
            debug_file_writer.write_to_file(
                f"pass_{current_pass_num - 1}.html",
                debug_file_writer.extract_html_content(response_text),
            )
            debug_file_writer.write_to_file(
                f"thinking_pass_{current_pass_num - 1}.txt",
                response_text.split("</thinking>")[0],
            )

        # Set up messages array for next pass
        messages += [
            {"role": "assistant", "content": str(prefix) + response.content[0].text},
            {
                "role": "user",
                "content": "You've done a good job with a first draft. Improve this further based on the original instructions so that the app is fully functional and looks like the original video of the app we're trying to replicate.",
            },
        ]

        print(
            f"Token usage: Input Tokens: {response.usage.input_tokens}, Output Tokens: {response.usage.output_tokens}"
        )

    # Close the Anthropic client
    await client.close()

    if IS_DEBUG_ENABLED:
        debug_file_writer.write_to_file("full_stream.txt", full_stream)

    if not response:
        raise Exception("No HTML response found in AI response")
    else:
        return response.content[0].text


async def stream_gemini_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:

    API_TYPE = "openai_compatible"

    if API_TYPE == "openai_compatible":
        return await generate_gemini_response_openai_compatible(
            messages, api_key, callback, model
        )
    elif API_TYPE == "google_generativeai":
        return await generate_gemini_response_google_generativeai(
            messages, api_key, callback, model
        )
    else:
        raise Exception(f"Invalid API type: {API_TYPE}")


# Disabled for now
async def generate_gemini_response_google_generativeai(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    return ""

    # import google.generativeai as genai

    # # # Extract image URLs from the message
    # image_urls = []
    # for content_part in messages[-1]["content"]:
    #     if content_part["type"] == "image_url":
    #         image_url = content_part["image_url"]["url"]
    #         if image_url.startswith("data:"):
    #             # Extract base64 data and mime type for data URLs
    #             mime_type = image_url.split(";")[0].split(":")[1]
    #             base64_data = image_url.split(",")[1]
    #             image_urls = [{"mime_type": mime_type, "data": base64_data}]
    #         else:
    #             # Store regular URLs
    #             image_urls = [{"uri": image_url}]
    #         break  # Exit after first image URL

    # # Print image URLs with truncated base64 data for debugging
    # for url in image_urls:
    #     if "data" in url:
    #         # Truncate base64 data to first 50 chars
    #         truncated_url = {
    #             "mime_type": url["mime_type"],
    #             "data": (
    #                 url["data"][:50] + "..." if len(url["data"]) > 50 else url["data"]
    #             ),
    #         }
    #         print("Image URL (base64):", truncated_url)
    #     else:
    #         print("Image URL:", url)

    # genai.configure(api_key=api_key)

    # gemini_model = genai.GenerativeModel(
    #     model.value,
    #     generation_config=genai.GenerationConfig(
    #         temperature=1.0,
    #         top_p=0.95,
    #         top_k=40,
    #         max_output_tokens=8192,
    #         response_mime_type="text/plain",
    #     ),
    # )

    # full_response = ""
    # async for response in await gemini_model.generate_content_async(
    #     [image_urls[0], messages[0]["content"]], stream=True
    # ):
    #     if response.text:
    #         full_response += response.text
    #         await callback(response.text)

    # return full_response


async def generate_gemini_response_openai_compatible(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    # Base parameters
    params = {
        "model": model.value,
        "messages": messages,
        "stream": True,
        "timeout": 600,
        "temperature": 0.0,
        "top_p": 0.95,
        # "top_k": 40,  # TODO: Not a valid param for openai?
        "max_tokens": 8192,
    }

    stream = await client.chat.completions.create(**params)  # type: ignore
    full_response = ""
    async for chunk in stream:  # type: ignore
        assert isinstance(chunk, ChatCompletionChunk)
        if (
            chunk.choices
            and len(chunk.choices) > 0
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ):
            content = chunk.choices[0].delta.content or ""
            full_response += content
            await callback(content)

    await client.close()

    return full_response
