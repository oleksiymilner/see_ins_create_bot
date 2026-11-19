import html
import logging
import os
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CONFLUENCE_BASE_URL = "https://oleksiymilner.atlassian.net/wiki"
ATLASSIAN_EMAIL = os.getenv("ATLASSIAN_EMAIL", "your-email@example.com")
ATLASSIAN_API_TOKEN = os.getenv("ATLASSIAN_API_TOKEN", "YOUR_ATLASSIAN_API_TOKEN")

# Confluence IDs extracted from your URLs
TEMPLATE_IDS = {
    "Sell Apartment contract": "1736705",
    "Rent Apartment contract": "1998849",
}
TARGET_FOLDER_ID = "458753"

# Placeholder keys used inside your Confluence templates
# (Update these if your templates use different placeholder names)
FIRST_NAME_PLACEHOLDER = "{{FIRST_NAME}}"
LAST_NAME_PLACEHOLDER = "{{LAST_NAME}}"

# Conversation states
CHOOSING_CONTRACT, TYPING_LASTNAME, TYPING_FIRSTNAME = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- CONFLUENCE API HELPERS ---
def get_confluence_auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN)


def fetch_template_page(page_id: str) -> dict:
    """Fetch template page storage XHTML and spaceId from Confluence v2 API."""
    url = f"{CONFLUENCE_BASE_URL}/api/v2/pages/{page_id}"
    params = {"body-format": "storage"}
    headers = {"Accept": "application/json"}

    response = requests.get(
        url, headers=headers, params=params, auth=get_confluence_auth(), timeout=15
    )
    response.raise_for_status()
    return response.json()


def create_contract_page(
    space_id: str, parent_folder_id: str, title: str, storage_body: str
) -> dict:
    """Create a new Confluence page inside the target folder."""
    url = f"{CONFLUENCE_BASE_URL}/api/v2/pages"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "spaceId": str(space_id),
        "status": "current",
        "title": title,
        "parentId": str(parent_folder_id),
        "body": {
            "representation": "storage",
            "value": storage_body,
        },
    }

    response = requests.post(
        url, headers=headers, json=payload, auth=get_confluence_auth(), timeout=15
    )
    response.raise_for_status()
    return response.json()


# --- TELEGRAM CONVERSATION HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [
        ["Sell Apartment contract"],
        ["Rent Apartment contract"],
    ]
    await update.message.reply_text(
        "Select the type of contract:",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CHOOSING_CONTRACT


async def contract_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contract_type = update.message.text.strip()
    if contract_type not in TEMPLATE_IDS:
        await update.message.reply_text(
            "Please choose one of the provided options using the keyboard."
        )
        return CHOOSING_CONTRACT

    context.user_data["contract_type"] = contract_type
    await update.message.reply_text(
        "Enter the client's Lastname:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TYPING_LASTNAME


async def lastname_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["lastname"] = update.message.text.strip()
    await update.message.reply_text("Enter the client's Firstname:")
    return TYPING_FIRSTNAME


async def firstname_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    firstname = update.message.text.strip()
    lastname = context.user_data["lastname"]
    contract_type = context.user_data["contract_type"]
    template_id = TEMPLATE_IDS[contract_type]

    await update.message.reply_text("Generating contract in Confluence...")

    try:
        # 1. Get template
        template_data = fetch_template_page(template_id)
        space_id = template_data["spaceId"]
        template_title = template_data["title"]
        body_html = template_data["body"]["storage"]["value"]

        # 2. Replace placeholders (escape XML/HTML special chars like &, <, >)
        safe_firstname = html.escape(firstname)
        safe_lastname = html.escape(lastname)

        updated_body = body_html.replace(FIRST_NAME_PLACEHOLDER, safe_firstname).replace(
            LAST_NAME_PLACEHOLDER, safe_lastname
        )

        # 3. Build a unique page title (Confluence requires unique titles per space)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_title = f"{template_title} - {lastname} {firstname} ({timestamp})"

        # 4. Create new page in folder 458753
        created_page = create_contract_page(
            space_id=space_id,
            parent_folder_id=TARGET_FOLDER_ID,
            title=new_title,
            storage_body=updated_body,
        )

        webui_path = created_page.get("_links", {}).get("webui", "")
        page_url = f"{CONFLUENCE_BASE_URL}{webui_path}"

        await update.message.reply_text(
            f"Contract created successfully!\n\n"
            f"Type: {contract_type}\n"
            f"Client: {lastname} {firstname}\n"
            f"Link: {page_url}"
        )

    except requests.HTTPError as e:
        error_detail = e.response.text if e.response is not None else str(e)
        logger.error("Confluence API error: %s", error_detail)
        await update.message.reply_text(
            f"Failed to create Confluence page (HTTP {e.response.status_code}):\n{error_detail[:500]}"
        )
    except Exception as e:
        logger.exception("Unexpected error")
        await update.message.reply_text(f"An error occurred: {e}")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Operation cancelled. Type /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_CONTRACT: [
                MessageHandler(
                    filters.Regex("^(Sell Apartment contract|Rent Apartment contract)$"),
                    contract_chosen,
                )
            ],
            TYPING_LASTNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, lastname_received)
            ],
            TYPING_FIRSTNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, firstname_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.run_polling()


if __name__ == "__main__":
    main()
