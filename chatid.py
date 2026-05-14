import telebot

bot = telebot.TeleBot('7667364714.......')

@bot.message_handler(commands=['start'])
def main(message):
    bot.send_message(message.chat.id, f'Ваш chat_id: {message.chat.id}')

bot.polling(none_stop=True)
