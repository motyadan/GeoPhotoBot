import telebot
import json
import mimetypes
import os
import re
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import quote

import requests
from telebot import types
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

# ============ НАСТРОЙКИ ============

TOKEN = os.getenv('TOKEN')
CHANNEL_ID = os.getenv('ID')
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN', '').strip()
YANDEX_DISK_FOLDER = os.getenv('YANDEX_DISK_FOLDER', 'GeoPhotoReports').strip().strip('/')

ALLOWED_USERS_FILE = 'allowed_users.json'
ADMINS_FILE = 'admins.json'
YANDEX_API_BASE = "https://cloud-api.yandex.net/v1/disk"

user_data = defaultdict(lambda: {"photos": [], "comment": ""})

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def get_russian_month_name(month_number):
    """Возвращает название месяца на русском"""
    months = {
        1: "Январь",
        2: "Февраль",
        3: "Март",
        4: "Апрель",
        5: "Май",
        6: "Июнь",
        7: "Июль",
        8: "Август",
        9: "Сентябрь",
        10: "Октябрь",
        11: "Ноябрь",
        12: "Декабрь"
    }
    return months.get(month_number, "Месяц")

def sanitize_for_path(name: str) -> str:
    """Заменяет все специальные символы на подчёркивание"""
    return re.sub(r'[\\/:*?"<>|#\s+,.-]', '_', name).strip('_')

def get_admins():
    try:
        with open(ADMINS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_admins(admins):
    with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
        json.dump(admins, f, ensure_ascii=False, indent=4)

def get_allowed_users():
    try:
        with open(ALLOWED_USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_allowed_users(users):
    with open(ALLOWED_USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=4)

def is_admin(user_id):
    return str(user_id) in get_admins()

def is_user_allowed(user_id):
    return str(user_id) in get_allowed_users()

def get_user_name(user_id):
    users = get_allowed_users()
    return users.get(str(user_id), "Неизвестный_пользователь")

def get_admin_name(admin_id):
    admins = get_admins()
    return admins.get(str(admin_id), "Неизвестный_админ")

class YandexDiskUploader:
    def __init__(self, token: str, root_folder: str):
        self.root_folder = root_folder
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"OAuth {token}",
                "Accept": "application/json",
            }
        )

    def ensure_folder_exists(self, disk_path: str):
        response = self.session.put(
            f"{YANDEX_API_BASE}/resources",
            params={"path": disk_path},
            timeout=30,
        )
        if response.status_code not in (201, 409):
            raise RuntimeError(f"Failed to create Yandex Disk folder: {response.status_code} {response.text}")

    def get_upload_url(self, disk_path: str) -> str:
        response = self.session.get(
            f"{YANDEX_API_BASE}/resources/upload",
            params={"path": disk_path, "overwrite": "true"},
            timeout=30,
        )
        response.raise_for_status()
        href = response.json().get("href")
        if not href:
            raise RuntimeError("Yandex Disk did not return an upload URL")
        return href

    def upload_file(self, local_path: str, relative_parts: list[str]) -> str:
        folder_parts = [part for part in relative_parts[:-1] if part]
        filename = relative_parts[-1]
        current_path = f"disk:/{self.root_folder}"

        self.ensure_folder_exists(current_path)
        for part in folder_parts:
            current_path = f"{current_path}/{part}"
            self.ensure_folder_exists(current_path)

        disk_path = f"{current_path}/{filename}"
        upload_url = self.get_upload_url(disk_path)

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(local_path, 'rb') as file_obj:
            response = self.session.put(
                upload_url,
                data=file_obj,
                headers={"Content-Type": content_type},
                timeout=300,
            )
            response.raise_for_status()

        encoded_path = quote(disk_path.replace("disk:/", ""), safe="/")
        return f"https://disk.yandex.ru/client/disk/{encoded_path}"

    def publish_path(self, disk_path: str) -> Optional[str]:
        publish_response = self.session.put(
            f"{YANDEX_API_BASE}/resources/publish",
            params={"path": disk_path},
            timeout=30,
        )
        if publish_response.status_code not in (200, 201, 202):
            raise RuntimeError(f"Failed to publish Yandex Disk path: {publish_response.status_code} {publish_response.text}")

        metadata_response = self.session.get(
            f"{YANDEX_API_BASE}/resources",
            params={"path": disk_path},
            timeout=30,
        )
        metadata_response.raise_for_status()
        return metadata_response.json().get("public_url")


yandex_uploader: Optional[YandexDiskUploader] = None


def init_yandex_uploader():
    global yandex_uploader
    if YANDEX_DISK_TOKEN:
        yandex_uploader = YandexDiskUploader(YANDEX_DISK_TOKEN, YANDEX_DISK_FOLDER)


def upload_report_file_to_cloud(local_path, safe_user_name, month_folder, report_folder_name):
    if not yandex_uploader:
        return

    relative_parts = [
        safe_user_name,
        month_folder,
        report_folder_name,
        Path(local_path).name,
    ]
    try:
        yandex_uploader.upload_file(local_path, relative_parts)
    except Exception as e:
        print(f"Ошибка при загрузке в Яндекс Диск {local_path}: {e}")


def get_user_disk_url(user_id) -> Optional[str]:
    if not yandex_uploader:
        return None

    safe_user_name = sanitize_for_path(get_user_name(user_id))
    try:
        return yandex_uploader.publish_path(f"disk:/{YANDEX_DISK_FOLDER}/{safe_user_name}")
    except Exception as e:
        print(f"Ошибка при открытии папки пользователя на Яндекс Диске: {e}")
        return None


def get_all_disk_url() -> Optional[str]:
    if not yandex_uploader:
        return None

    try:
        return yandex_uploader.publish_path(f"disk:/{YANDEX_DISK_FOLDER}")
    except Exception as e:
        print(f"Ошибка при открытии общей папки на Яндекс Диске: {e}")
        return None


def save_photos_thread(photos, safe_user_name, safe_comment, chat_id_str, timestamp):
    now = datetime.now()
    month_name = get_russian_month_name(now.month)
    month_folder = f"{month_name}_{now.year}"
    date_str = now.strftime("%d-%m")

    report_folder_name = f"{date_str}_{safe_comment}" if len(safe_comment) < 50 else f"{date_str}_report"
    report_folder_name = sanitize_for_path(report_folder_name)
    public_url = None

    for idx, file_id in enumerate(photos, start=1):
        temp_path = None
        try:
            file_info = bot.get_file(file_id)
            file_path = file_info.file_path
            photo_data = bot.download_file(file_path)
            filename = f"{chat_id_str}-{timestamp}-{idx}.jpg"

            with NamedTemporaryFile(delete=False, suffix=".jpg", prefix="report_") as temp_file:
                temp_file.write(photo_data)
                temp_path = temp_file.name

            upload_report_file_to_cloud(temp_path, safe_user_name, month_folder, report_folder_name)
        except Exception as e:
            print(f"Ошибка при сохранении {file_id}: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    if yandex_uploader:
        try:
            public_url = yandex_uploader.publish_path(
                f"disk:/{YANDEX_DISK_FOLDER}/{safe_user_name}/{month_folder}/{report_folder_name}"
            )
        except Exception as e:
            print(f"Failed to publish Yandex Disk folder: {e}")

    return public_url

bot = telebot.TeleBot(TOKEN)

def send_main_menu(chat_id, user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = []
    if is_admin(user_id):
        buttons.append(types.KeyboardButton("Админ панель"))
    if is_user_allowed(user_id):
        buttons.append(types.KeyboardButton("Сделать фотоотчёт"))
    markup.add(*buttons)
    bot.send_message(chat_id, "Выберите действие:", reply_markup=markup)

@bot.message_handler(commands=['start'])
def start(message):
    send_main_menu(message.chat.id, message.from_user.id)


@bot.message_handler(commands=['mydisk'])
def mydisk(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    if not yandex_uploader:
        return bot.reply_to(message, "Яндекс Диск не настроен.")

    user_name = get_user_name(message.from_user.id)
    public_url = get_user_disk_url(message.from_user.id)
    if not public_url:
        return bot.reply_to(message, "Не удалось открыть папку на Яндекс Диске.")

    bot.send_message(
        message.chat.id,
        f'{user_name}: <a href="{public_url}">Яндекс.Диск</a>',
        parse_mode='HTML',
    )


@bot.message_handler(commands=['alldisk'])
def alldisk(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    if not yandex_uploader:
        return bot.reply_to(message, "Яндекс Диск не настроен.")

    public_url = get_all_disk_url()
    if not public_url:
        return bot.reply_to(message, "Не удалось открыть общую папку на Яндекс Диске.")

    bot.send_message(
        message.chat.id,
        f'<a href="{public_url}">Яндекс.Диск</a>',
        parse_mode='HTML',
    )

# ============ АДМИН-ПАНЕЛЬ ============

@bot.message_handler(func=lambda msg: msg.text == "Админ панель")
def admin_panel(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "У вас нет прав доступа.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Управление пользователями", "Управление админами")
    markup.add("Назад")
    bot.send_message(message.chat.id, "Админ панель:", reply_markup=markup)


@bot.message_handler(func=lambda msg: msg.text == "Назад")
def back_to_main(message):
    send_main_menu(message.chat.id, message.from_user.id)


# ============ УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ============

@bot.message_handler(func=lambda msg: msg.text == "Управление пользователями")
def manage_users(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Добавить пользователя", "Список пользователей")
    markup.add("Удалить пользователя", "Назад")
    bot.send_message(message.chat.id, "Управление пользователями:", reply_markup=markup)


@bot.message_handler(func=lambda msg: msg.text == "Добавить пользователя")
def add_user_request(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет прав на добавление пользователей.")
    msg = bot.send_message(
        message.chat.id,
        "Введите ID и имя пользователя в формате: chat_id имя"
    )
    bot.register_next_step_handler(msg, add_user_by_text)


def add_user_by_text(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет прав на добавление!")
    try:
        chat_id, name = message.text.split(maxsplit=1)
        users = get_allowed_users()
        users[chat_id] = name
        save_allowed_users(users)
        bot.reply_to(message, f"Пользователь '{name}' добавлен.")
    except ValueError:
        bot.reply_to(message, "Ошибка формата. Используйте: chat_id имя")
    time.sleep(1)
    manage_users(message)


@bot.message_handler(func=lambda msg: msg.text == "Список пользователей")
def show_user_list(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    users = get_allowed_users()
    if not users:
        text = "Список пользователей пуст."
    else:
        text = "Список пользователей:\n"
        for uid, name in users.items():
            text += f"{uid} — {name}\n"
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda msg: msg.text == "Удалить пользователя")
def delete_user_request(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    users = get_allowed_users()
    if not users:
        return bot.send_message(message.chat.id, "Список пользователей пуст.")

    markup = types.InlineKeyboardMarkup()
    for uid, name in users.items():
        markup.add(types.InlineKeyboardButton(text=name, callback_data=f"del_user_{uid}"))

    bot.send_message(message.chat.id, "Выберите пользователя для удаления:", reply_markup=markup)


# ============ УПРАВЛЕНИЕ АДМИНАМИ ============

@bot.message_handler(func=lambda msg: msg.text == "Управление админами")
def manage_admins(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Добавить админа", "Список админов")
    markup.add("Удалить админа", "Назад")
    bot.send_message(message.chat.id, "Управление админами:", reply_markup=markup)


@bot.message_handler(func=lambda msg: msg.text == "Добавить админа")
def add_admin_request(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет прав на добавление админов.")
    msg = bot.send_message(
        message.chat.id,
        "Введите ID и имя админа в формате: chat_id имя"
    )
    bot.register_next_step_handler(msg, add_admin_by_text)


def add_admin_by_text(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет прав на добавление!")
    try:
        admin_id, name = message.text.split(maxsplit=1)
        admins = get_admins()
        admins[admin_id] = name
        save_admins(admins)
        bot.reply_to(message, f"Админ '{name}' добавлен.")
    except ValueError:
        bot.reply_to(message, "Ошибка формата. Используйте: chat_id имя")
    time.sleep(1)
    manage_admins(message)


@bot.message_handler(func=lambda msg: msg.text == "Список админов")
def show_admin_list(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    admins = get_admins()
    if not admins:
        text = "Список админов пуст."
    else:
        text = "Список админов:\n"
        for uid, name in admins.items():
            text += f"{uid} — {name}\n"
    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda msg: msg.text == "Удалить админа")
def delete_admin_request(message):
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    admins = get_admins()
    if not admins:
        return bot.send_message(message.chat.id, "Список админов пуст.")

    markup = types.InlineKeyboardMarkup()
    for uid, name in admins.items():
        markup.add(types.InlineKeyboardButton(text=f"{name} ({uid})", callback_data=f"del_admin_{uid}"))

    bot.send_message(message.chat.id, "Выберите админа для удаления:", reply_markup=markup)


# ============ INLINE-КНОПКИ ============

@bot.callback_query_handler(func=lambda call: call.data.startswith(("del_user_", "del_admin_")))
def handle_delete(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    if call.data.startswith("del_user_"):
        user_id_to_delete = call.data[len("del_user_"):]
        users = get_allowed_users()
        if user_id_to_delete in users:
            user_name = users.pop(user_id_to_delete)
            save_allowed_users(users)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Пользователь '{user_name}' удалён."
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Пользователь не найден."
            )

    elif call.data.startswith("del_admin_"):
        admin_id_to_delete = call.data[len("del_admin_"):]
        admins = get_admins()
        if admin_id_to_delete in admins:
            admin_name = admins.pop(admin_id_to_delete)
            save_admins(admins)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Админ '{admin_name}' удалён."
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Админ не найден."
            )

    bot.answer_callback_query(call.id)


# ============ ОТЧЁТЫ ============

@bot.message_handler(func=lambda msg: msg.text == "Сделать фотоотчёт")
def report_start(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    uid = str(message.from_user.id)
    user_data[uid] = {"photos": [], "comment": ""}

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Отмена"))

    msg = bot.send_message(
        message.chat.id,
        "Какой объект (комментарий)?",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, handle_comment)


def handle_comment(message):
    if message.text == "Отмена":
        return cancel_report(message)

    uid = str(message.from_user.id)
    user_data[uid]["comment"] = message.text

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Завершить отчёт"))
    markup.add(types.KeyboardButton("Отмена"))

    bot.send_message(
        message.chat.id,
        "Теперь отправь фото. Когда закончишь, нажми 'Завершить отчёт'.",
        reply_markup=markup
    )


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    uid = str(message.from_user.id)
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа для отправки фото.")
    file_id = message.photo[-1].file_id
    user_data[uid]["photos"].append(file_id)

@bot.message_handler(func=lambda msg: msg.text == "Завершить отчёт")
def finish_report(message):
    uid = str(message.from_user.id)
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    photos = user_data[uid]["photos"]
    comment_raw = user_data[uid]["comment"]
    name_raw = get_user_name(uid)
    chat_id_str = uid

    if not comment_raw or comment_raw.strip() == "":
        return bot.send_message(message.chat.id, "Нужно ввести комментарий к фотоотчёту. Начните заново с кнопки 'Сделать фотоотчёт'.")

    if not photos:
        return bot.send_message(message.chat.id, "Вы не отправили ни одной фотографии.")

    sending_msg = bot.send_message(message.chat.id, "Отправка...")

    safe_user_name = sanitize_for_path(name_raw)
    safe_comment = sanitize_for_path(comment_raw)
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    photos_copy = list(photos)
    public_url = save_photos_thread(photos_copy, safe_user_name, safe_comment, chat_id_str, timestamp)

    caption = f"<b>Фотоотчёт от {name_raw}</b>\nОбъект: {comment_raw}"
    if public_url:
        caption += f'\n<a href="{public_url}">Яндекс.Диск</a>'

    chunk_size = 10
    for i in range(0, len(photos), chunk_size):
        group = photos[i:i + chunk_size]
        media_group = []
        for j, file_id in enumerate(group):
            if j == 0:
                media_group.append(types.InputMediaPhoto(media=file_id, caption=caption, parse_mode='HTML'))
            else:
                media_group.append(types.InputMediaPhoto(media=file_id))
        bot.send_media_group(CHANNEL_ID, media_group)

    user_data[uid] = {"photos": [], "comment": ""}

    user_message = "Отчёт отправлен в канал."
    if public_url:
        user_message += f'\n<a href="{public_url}">Яндекс.Диск</a>'

    bot.edit_message_text(user_message, chat_id=message.chat.id, message_id=sending_msg.message_id, parse_mode='HTML')
    send_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda msg: msg.text in ["Отменить отчёт", "Отмена"])
def cancel_report(message):
    uid = str(message.from_user.id)
    user_data[uid] = {"photos": [], "comment": ""}
    bot.send_message(message.chat.id, "Отчёт отменён.")
    send_main_menu(message.chat.id, message.from_user.id)

# ============ ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ============

@bot.message_handler(func=lambda msg: True)
def fallback(message):
    send_main_menu(message.chat.id, message.from_user.id)

# ============ ЗАПУСК БОТА ============

if __name__ == '__main__':
    load_dotenv()
    init_yandex_uploader()
    # Создаем пустой файл admins.json, если его нет
    if not os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    
    # Создаем пустой файл allowed_users.json, если его нет
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    
    print("Бот запущен.")
    bot.infinity_polling()
