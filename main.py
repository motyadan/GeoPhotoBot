import telebot
import json
import html
import mimetypes
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import quote

import requests
from telebot import types
from collections import defaultdict
import threading
from dotenv import load_dotenv
load_dotenv()

# ============ НАСТРОЙКИ ============

TOKEN = os.getenv('TOKEN')
CHANNEL_ID = os.getenv('ID')
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN', '').strip()
YANDEX_DISK_FOLDER = os.getenv('YANDEX_DISK_FOLDER', 'GeoPhotoReports').strip().strip('/')

ALLOWED_USERS_FILE = 'allowed_users.json'
ADMINS_FILE = 'admins.json'
SENSORS_FILE = 'sensors.json'
YANDEX_API_BASE = "https://cloud-api.yandex.net/v1/disk"

user_data = defaultdict(lambda: {"media": [], "comment": "", "report_type": "media", "sensor_name": ""})

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

def compress_video(source_path: str) -> str:
    """Сжимает видео для ускорения загрузки, если доступен ffmpeg."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return source_path

    dest_path = f"{source_path}.compressed.mp4"
    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-i",
                source_path,
                "-vf",
                "scale=640:-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                dest_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return dest_path
    except Exception as e:
        print(f"Video compression failed: {e}")
        return source_path


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


def get_sensors():
    try:
        with open(SENSORS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_sensors(sensors):
    with open(SENSORS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sensors, f, ensure_ascii=False, indent=4)


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


def upload_report_file_to_cloud(local_path, relative_parts):
    if not yandex_uploader:
        return

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


def get_sensors_disk_url() -> Optional[str]:
    """Возвращает публичную ссылку на папку с датчиками на Яндекс.Диске"""
    if not yandex_uploader:
        return None

    try:
        return yandex_uploader.publish_path(f"disk:/{YANDEX_DISK_FOLDER}/Датчики")
    except Exception as e:
        print(f"Ошибка при открытии папки 'Датчики' на Яндекс Диске: {e}")
        return None


def save_media_thread(media_items, safe_user_name, safe_comment, chat_id_str, timestamp):
    now = datetime.now()
    month_name = get_russian_month_name(now.month)
    month_folder = f"{month_name}_{now.year}"
    date_str = now.strftime("%d-%m")

    report_folder_name = f"{date_str}_{safe_comment}" if len(safe_comment) < 50 else f"{date_str}_report"
    report_folder_name = sanitize_for_path(report_folder_name)
    public_url = None

    for idx, item in enumerate(media_items, start=1):
        temp_path = None
        compressed_path = None
        try:
            file_info = bot.get_file(item["file_id"])
            file_path = file_info.file_path
            file_data = bot.download_file(file_path)
            suffix = Path(file_path).suffix
            if not suffix:
                suffix = ".mp4" if item["type"] == "video" else ".jpg"
            filename = f"{chat_id_str}-{timestamp}-{idx}{suffix}"

            with NamedTemporaryFile(delete=False, suffix=suffix, prefix="report_") as temp_file:
                temp_file.write(file_data)
                temp_path = temp_file.name

            upload_path = temp_path
            if item["type"] == "video":
                compressed_path = compress_video(temp_path)
                upload_path = compressed_path

            upload_report_file_to_cloud(upload_path, [safe_user_name, month_folder, report_folder_name, filename])
        except Exception as e:
            print(f"Ошибка при сохранении {item['file_id']}: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if compressed_path and compressed_path != temp_path and os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
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


def save_sensor_media_thread(media_items, sensor_name, timestamp):
    now = datetime.now()
    date_str = now.strftime("%d-%m-%Y")
    report_folder_name = sanitize_for_path(f"{date_str}_отчет")
    safe_sensor_name = sanitize_for_path(sensor_name)
    public_url = None

    for idx, item in enumerate(media_items, start=1):
        temp_path = None
        compressed_path = None
        try:
            file_info = bot.get_file(item["file_id"])
            file_path = file_info.file_path
            file_data = bot.download_file(file_path)
            suffix = Path(file_path).suffix
            if not suffix:
                suffix = ".mp4" if item["type"] == "video" else ".jpg"
            filename = f"{safe_sensor_name}-{timestamp}-{idx}{suffix}"

            with NamedTemporaryFile(delete=False, suffix=suffix, prefix="report_") as temp_file:
                temp_file.write(file_data)
                temp_path = temp_file.name

            upload_path = temp_path
            if item["type"] == "video":
                compressed_path = compress_video(temp_path)
                upload_path = compressed_path

            upload_report_file_to_cloud(upload_path, ["Датчики", safe_sensor_name, report_folder_name, filename])
        except Exception as e:
            print(f"Ошибка при сохранении {item['file_id']}: {e}")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if compressed_path and compressed_path != temp_path and os.path.exists(compressed_path):
                try:
                    os.remove(compressed_path)
                except OSError:
                    pass

    if yandex_uploader:
        try:
            public_url = yandex_uploader.publish_path(
                f"disk:/{YANDEX_DISK_FOLDER}/Датчики/{safe_sensor_name}/{report_folder_name}"
            )
        except Exception as e:
            print(f"Failed to publish Yandex Disk folder: {e}")

    return public_url

bot = telebot.TeleBot(TOKEN)

def send_main_menu(chat_id, user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # Большая основная кнопка для фото/медиа отчёта
    if is_user_allowed(user_id):
        markup.add(types.KeyboardButton("Сделать фотоотчёт"))

    # Компактные дополнительные кнопки
    small_buttons = []
    if is_admin(user_id):
        small_buttons.append(types.KeyboardButton("Админ панель"))
    if is_user_allowed(user_id):
        small_buttons.append(types.KeyboardButton("Датчики"))
    if small_buttons:
        markup.add(*small_buttons)

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


@bot.message_handler(commands=['sensors'])
def sensors_command(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    if not yandex_uploader:
        return bot.reply_to(message, "Яндекс Диск не настроен.")

    public_url = get_sensors_disk_url()
    if not public_url:
        return bot.reply_to(message, "Не удалось открыть папку с датчиками на Яндекс.Диске.")

    bot.send_message(
        message.chat.id,
        f'<a href="{public_url}">Папка Датчики на Яндекс.Диске</a>',
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


@bot.message_handler(func=lambda msg: msg.text == "Добавить объект")
def add_sensor_object_request(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Отмена"))
    msg = bot.send_message(
        message.chat.id,
        "Введите название объекта для датчиков:",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, create_sensor_object)


@bot.message_handler(func=lambda msg: msg.text == "Удалить объект")
def delete_sensor_request(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    sensors = get_sensors()
    if not sensors:
        return bot.send_message(message.chat.id, "Список объектов пуст. Добавьте новый объект.")

    markup = types.InlineKeyboardMarkup()
    for index, sensor in enumerate(sensors):
        markup.add(types.InlineKeyboardButton(text=sensor, callback_data=f"del_sensor_{index}"))
    markup.add(types.InlineKeyboardButton(text="Отмена", callback_data="cancel_sensor_action"))

    bot.send_message(message.chat.id, "Выберите объект для удаления:", reply_markup=markup)


def create_sensor_object(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    name = message.text.strip()
    if not name:
        return bot.reply_to(message, "Название объекта не может быть пустым.")

    if name.lower() == "отмена":
        bot.reply_to(message, "Добавление объекта отменено.")
        return sensors_menu(message)

    sensors = get_sensors()
    if name in sensors:
        bot.reply_to(message, f"Объект '{name}' уже существует.")
    else:
        sensors.append(name)
        save_sensors(sensors)
        bot.reply_to(message, f"Объект '{name}' добавлен.")

    sensors_menu(message)


@bot.message_handler(func=lambda msg: msg.text == "Датчики")
def sensors_menu(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    sensors = get_sensors()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if sensors:
        for sensor in sensors:
            markup.add(types.KeyboardButton(sensor))
    else:
        bot.send_message(message.chat.id, "Список объектов пуст. Добавьте новый объект.")
    markup.add(types.KeyboardButton("Добавить объект"))
    markup.add(types.KeyboardButton("Удалить объект"))
    markup.add(types.KeyboardButton("Назад"))
    bot.send_message(
        message.chat.id,
        "Выберите объект для отчёта по датчикам или добавьте/удалите объект.",
        reply_markup=markup
    )


@bot.message_handler(func=lambda msg: msg.text in get_sensors())
def select_sensor_object(message):
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")
    uid = str(message.from_user.id)
    user_data[uid] = {
        "media": [],
        "comment": "",
        "report_type": "sensors",
        "sensor_name": message.text,
    }
    # Сначала запрашиваем комментарий (например, номер сваи) с опцией пропустить
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("Пропустить"), types.KeyboardButton("Отмена"))
    msg = bot.send_message(
        message.chat.id,
        "Напишите комментарий (например, номер сваи) или нажмите Пропустить",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, handle_sensor_comment)


def handle_sensor_comment(message):
    text = (message.text or "").strip()
    if text.lower() == "отмена":
        return cancel_report(message)

    uid = str(message.from_user.id)
    # Возможность пропустить ввод комментария
    if text.lower() in ("пропустить", "/skip", "/пропустить"):
        user_data[uid]["comment"] = ""
    else:
        # Сохраняем комментарий и предлагаем отправить медиа
        user_data[uid]["comment"] = message.text

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Завершить отчёт"))
    markup.add(types.KeyboardButton("Отменить отчёт"))

    bot.send_message(
        message.chat.id,
        f"Теперь отправь фото или видео для отчёта по датчику '{user_data[uid]['sensor_name']}'. Когда закончишь, нажми 'Завершить отчёт'.",
        reply_markup=markup
    )


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

@bot.callback_query_handler(func=lambda call: call.data.startswith(("del_user_", "del_admin_", "del_sensor_", "cancel_sensor_action")))
def handle_delete(call):
    if call.data == "cancel_sensor_action":
        if not is_user_allowed(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа.")
            return

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Удаление объекта отменено."
        )
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("del_sensor_"):
        if not is_user_allowed(call.from_user.id):
            bot.answer_callback_query(call.id, "Нет доступа.")
            return

        try:
            sensor_index = int(call.data[len("del_sensor_"):])
        except ValueError:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Неверный объект для удаления."
            )
            bot.answer_callback_query(call.id)
            return

        sensors = get_sensors()
        if 0 <= sensor_index < len(sensors):
            sensor_name = sensors.pop(sensor_index)
            save_sensors(sensors)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"Объект '{sensor_name}' удалён."
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="Объект не найден."
            )

        bot.answer_callback_query(call.id)
        return

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
    user_data[uid] = {"media": [], "comment": "", "report_type": "media", "sensor_name": ""}

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
        "Теперь отправь фото или видео. Когда закончишь, нажми 'Завершить отчёт'.",
        reply_markup=markup
    )


@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    uid = str(message.from_user.id)
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа для отправки медиа.")

    if message.content_type == 'photo':
        file_id = message.photo[-1].file_id
        user_data[uid]["media"].append({"type": "photo", "file_id": file_id})
    elif message.content_type == 'video':
        user_data[uid]["media"].append({"type": "video", "file_id": message.video.file_id})

@bot.message_handler(func=lambda msg: msg.text == "Завершить отчёт")
def finish_report(message):
    uid = str(message.from_user.id)
    if not is_user_allowed(message.from_user.id):
        return bot.reply_to(message, "Нет доступа.")

    # Защита от повторного нажатия: если отправка уже запущена, игнорируем
    if user_data[uid].get("locked"):
        return bot.send_message(message.chat.id, "Отчёт уже отправляется, подождите...")

    media_items = user_data[uid]["media"]
    report_type = user_data[uid].get("report_type", "media")
    comment_raw = user_data[uid].get("comment", "")
    name_raw = get_user_name(uid)
    chat_id_str = uid

    if report_type == "media":
        if not comment_raw or comment_raw.strip() == "":
            return bot.send_message(message.chat.id, "Нужно ввести комментарий к отчёту. Начните заново с кнопки 'Сделать фотоотчёт'.")

    if not media_items:
        return bot.send_message(message.chat.id, "Вы не отправили ни одного медиафайла.")

    # Помечаем, что отправка началась (защита от повторных нажатий во время отправки)
    user_data[uid]["locked"] = True
    sending_msg = bot.send_message(message.chat.id, "Отправка...")
    public_url = None
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    media_copy = list(media_items)

    if report_type == "media":
        safe_name = html.escape(name_raw)
        safe_comment = html.escape(comment_raw)
        caption = f"<b>Отчёт от {safe_name}</b>\nОбъект: {safe_comment}"
    else:
        sensor_name = user_data[uid].get("sensor_name", "")
        if not sensor_name:
            return bot.send_message(message.chat.id, "Не выбран объект для отчёта по датчикам. Начните заново.")
        safe_sensor_name = html.escape(sensor_name)
        safe_name = html.escape(name_raw)
        safe_comment = html.escape(comment_raw)
        comment_line = f"\nКомментарий: {safe_comment}" if safe_comment else ""
        caption = f"<b>{safe_sensor_name}</b> - отчет по датчикам ({safe_name}){comment_line}"

    # Отправляем медиа в канал сразу (без ожидания загрузки на Диск)
    chunk_size = 10
    for i in range(0, len(media_items), chunk_size):
        group = media_items[i:i + chunk_size]
        media_group = []
        for j, item in enumerate(group):
            if item["type"] == "photo":
                if j == 0:
                    media_group.append(types.InputMediaPhoto(media=item["file_id"], caption=caption, parse_mode='HTML'))
                else:
                    media_group.append(types.InputMediaPhoto(media=item["file_id"]))
            else:
                if j == 0:
                    media_group.append(types.InputMediaVideo(media=item["file_id"], caption=caption, parse_mode='HTML'))
                else:
                    media_group.append(types.InputMediaVideo(media=item["file_id"]))
        bot.send_media_group(CHANNEL_ID, media_group)

    # Снимаем локальную блокировку — отправка в канал завершена
    user_data[uid]["locked"] = False

    # Фоновая загрузка на Яндекс.Диск (не блокирует отправку в канал)
    def background_upload():
        try:
            if report_type == "media":
                safe_user_name = sanitize_for_path(name_raw)
                safe_comment = sanitize_for_path(comment_raw)
                url = save_media_thread(media_copy, safe_user_name, safe_comment, chat_id_str, timestamp)
            else:
                sensor_name = user_data[uid].get("sensor_name", "")
                url = save_sensor_media_thread(media_copy, sensor_name, timestamp)

            if url:
                try:
                    if report_type == "media":
                        channel_text = f'<b>Отчёт от {name_raw}</b>\nОбъект: {comment_raw}\n<a href="{url}">Яндекс.Диск</a>'
                    else:
                        comment_line = f"\nКомментарий: {html.escape(comment_raw)}" if comment_raw else ""
                        channel_text = f'<b>{sensor_name}</b> - отчет по датчикам ({name_raw}){comment_line}\n<a href="{url}">Яндекс.Диск</a>'
                    bot.send_message(CHANNEL_ID, channel_text, parse_mode='HTML')
                except Exception:
                    pass
                try:
                    bot.send_message(message.chat.id, f'Отчёт отправлен в канал.\n<a href="{url}">Яндекс.Диск</a>', parse_mode='HTML')
                except Exception:
                    pass
        except Exception as e:
            print(f"Background upload failed: {e}")

    threading.Thread(target=background_upload, daemon=True).start()

    user_data[uid] = {"media": [], "comment": "", "report_type": "media", "sensor_name": ""}

    # Очистим возможные зарегистрированные "next step" обработчики
    try:
        bot.clear_step_handler(message)
    except Exception:
        try:
            bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception:
            pass

    # Удаляем предыдущую клавиатуру прежде чем показать главное меню
    try:
        bot.send_message(message.chat.id, " ", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        pass

    send_main_menu(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda msg: (msg.text or "").strip() in ["Отменить отчёт", "Отменить отчет", "Отмена"])
def cancel_report(message):
    uid = str(message.from_user.id)
    user_data[uid] = {"media": [], "comment": "", "report_type": "media", "sensor_name": ""}
    # Очистим возможные зарегистрированные "next step" обработчики, чтобы не осталось висящих шагов
    try:
        bot.clear_step_handler(message)
    except Exception:
        try:
            bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception:
            pass

    # Удаляем клавиатуру и возвращаем пользователя в главное меню
    try:
        bot.send_message(message.chat.id, "Отчёт отменён.", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
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
    if not os.path.exists(SENSORS_FILE):
        with open(SENSORS_FILE, 'w', encoding='utf-8') as f:
            json.dump(["Датчик 1", "Датчик 2", "Датчик 3"], f, ensure_ascii=False, indent=4)
    if not os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    
    # Создаем пустой файл allowed_users.json, если его нет
    if not os.path.exists(ALLOWED_USERS_FILE):
        with open(ALLOWED_USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=4)
    
    print("Бот запущен.")
    bot.infinity_polling()
