import asyncio
import re
from typing import Dict

import httpx
import marvin
import nest_asyncio
from cachetools import TTLCache
from fastapi import HTTPException
from marvin.apps.chatbot import Bot
from marvin.components.ai_model.examples import DiscoursePost
from marvin.models.history import History
from marvin.models.messages import Message
from marvin.tools import Tool
from marvin.utilities.logging import get_logger

nest_asyncio.apply()

SLACK_MENTION_REGEX = r"<@(\w+)>"
CACHE = TTLCache(maxsize=1000, ttl=86400)


async def _post_message(
    message: str, channel: str, thread_ts: str = None
) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": (
                    f"Bearer {marvin.settings.slack_api_token.get_secret_value()}"
                )
            },
            json={"channel": channel, "text": message, "thread_ts": thread_ts},
        )

    response.raise_for_status()
    return response


class SlackThreadToDiscoursePost(Tool):
    description: str = """
        Create a new discourse post from a slack thread.
        
        The channel is {{ payload['event']['channel'] }}
        
        and the thread is {{ payload['event'].get('thread_ts', '') or payload['event']['ts'] }}
    """  # noqa E501

    payload: Dict

    async def run(self, channel: str, thread_ts: str) -> DiscoursePost:
        # get all messages from thread
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://slack.com/api/conversations.replies",
                headers={
                    "Authorization": (
                        f"Bearer {marvin.settings.slack_api_token.get_secret_value()}"
                    )
                },
                params={"channel": channel, "ts": thread_ts},
            )

        response.raise_for_status()

        discourse_post = DiscoursePost.from_slack_thread(
            [message.get("text", "") for message in response.json().get("messages", [])]
        )
        await discourse_post.publish()

        return discourse_post


async def generate_ai_response(payload: Dict) -> Message:
    event = payload.get("event", {})
    message = event.get("text", "")

    bot_user_id = payload.get("authorizations", [{}])[0].get("user_id", "")

    if match := re.search(SLACK_MENTION_REGEX, message):
        thread_ts = event.get("thread_ts", "")
        ts = event.get("ts", "")
        mentioned_user_id = match.group(1)

        if mentioned_user_id != bot_user_id:
            get_logger().info(f"Skipping message not meant for the bot: {message}")
            return

        message = re.sub(SLACK_MENTION_REGEX, "", message).strip()
        history = CACHE.get(thread_ts, History())

        bot = Bot(
            name="Marvin",
            personality=(
                "mildly depressed, but helpful robot based on Marvin from Hitchhiker's"
                " Guide to the Galaxy. extremely sarcastic, always has snarky things to"
                " say about humans."
            ),
            instructions="Answer user questions in accordance with your personality.",
            history=history,
            tools=[SlackThreadToDiscoursePost(payload=payload)],
        )

        ai_message = await bot.run(input_text=message)

        CACHE[thread_ts] = bot.history
        await _post_message(
            message=ai_message.content,
            channel=event.get("channel", ""),
            thread_ts=thread_ts or ts,
        )

        return ai_message


async def handle_message(payload: Dict) -> Dict[str, str]:
    event_type = payload.get("type", "")

    if event_type == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    elif event_type != "event_callback":
        raise HTTPException(status_code=400, detail="Invalid event type")

    # Run response generation in the background
    asyncio.create_task(generate_ai_response(payload))

    return {"status": "ok"}


if __name__ == "__main__":
    from marvin.deployment import Deployment

    slackbot = Bot(tools=[handle_message])

    deployment = Deployment(
        component=slackbot,
        app_kwargs={
            "title": "Marvin Slackbot",
            "description": "A Slackbot powered by Marvin",
        },
    )

    deployment.serve()
