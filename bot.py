import datetime
import io
import logging
import os
import pickle
import secrets
import re
import uuid
from hashlib import sha256
from typing import Optional

import django
import telegram
from django.db import transaction
from dotenv import load_dotenv
from redis import Redis
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, JobQueue
from telegram.ext.dispatcher import run_async

import dice

load_dotenv()
django.setup()

from archive.models import Chat, Log, LogKind  # noqa
from game.models import Round, Actor, Player  # noqa


# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
redis = Redis(host='redis', port=6379, db=0)
DEFAULT_FACE = 100
BUFFER_TIME = 20
TOKEN = os.environ['BOT_TOKEN']
GM_SYMBOL = '✧'

logger = logging.getLogger(__name__)


help_file = open('./help.md')
start_file = open('./start.md')
HELP_TEXT = help_file.read()
START_TEXT = start_file.read()
help_file.close()
start_file.close()

ROUND_REPLY_MARKUP = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("删除", callback_data='round:remove'),
        InlineKeyboardButton("结束", callback_data='round:finish'),
        InlineKeyboardButton("←", callback_data='round:prev'),
        InlineKeyboardButton("→", callback_data='round:next'),
    ],
])


class NotGm(Exception):
    pass


def is_valid_chat_type(chat: telegram.Chat) -> bool:
    return isinstance(chat, telegram.Chat) and chat.type in ('supergroup', 'group')


def start_command(_, update, job_queue):
    """Send a message when the command /start is issued."""
    message = update.message
    assert isinstance(message, telegram.Message)
    if not is_valid_chat_type(message.chat):
        message.reply_text(START_TEXT, parse_mode='Markdown')
        return
    chat = get_chat(message.chat)
    if not chat.recording:
        chat.recording = True
        chat.save()
        message.chat.send_message('#start 已重新开始记录，输入 /save 告一段落')
    else:
        error_message(message, job_queue, '已经正在记录了')


def save(_, update, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if not is_valid_chat_type(message.chat):
        return
    chat = get_chat(message.chat)
    if chat.recording:
        chat.recording = False
        chat.save_date = datetime.datetime.now()
        chat.save()
        message.chat.send_message('#save 告一段落，在 /start 前我不会再记录')
    else:
        error_message(message, job_queue, '已经停止记录了')


def delete_message(message: telegram.Message):
    try:
        message.delete()
    except TelegramError:
        try:
            message.reply_text('删除消息失败，请检查一下 bot 的权限设置')
        except TelegramError:
            pass


def bot_help(_, update):
    """Send a message when the command /help is issued."""
    update.message.reply_text(HELP_TEXT, parse_mode='HTML')


def set_temp_name(chat_id, user_id, temp_name):
    player = Player.objects.filter(chat_id=chat_id, user_id=user_id).first()
    if player:
        player.temp_character_name = temp_name
        player.save()


def get_temp_name(chat_id, user_id):
    player = Player.objects.filter(chat_id=chat_id, user_id=user_id).first()
    if player:
        return player.temp_character_name or ''


def round_inline_callback(bot: telegram.Bot, query: telegram.CallbackQuery, gm: bool):
    game_round = Round.objects.filter(chat_id=query.message.chat_id).first()
    if not isinstance(game_round, Round):
        query.answer(show_alert=True, text='现在游戏没在回合状态之中')
        return
    method = str(query.data)
    actors = game_round.get_actors()
    if method == 'round:next':
        next_count = game_round.counter + 1
        if next_count >= len(actors):
            next_count = 0
            game_round.round_counter += 1
        game_round.counter = next_count
        game_round.save()
        query.answer()
        refresh_round_message(bot, game_round)
    elif method == 'round:prev':
        prev_count = game_round.counter - 1
        if prev_count < 0:
            if game_round.round_counter <= 1:
                query.answer(text='已经是第一回合了')
                return
            else:
                prev_count = len(actors) - 1
                game_round.round_counter -= 1
        game_round.counter = prev_count
        query.answer()
        refresh_round_message(bot, game_round, refresh=True)
        game_round.save()
    elif method == 'round:remove':
        if not gm:
            raise NotGm()

        actors = game_round.get_actors()
        if len(actors) > 1:
            current = actors[game_round.counter % len(actors)]
            current.delete()
            query.answer()
            refresh_round_message(bot, game_round, refresh=True)
        else:
            query.answer(show_alert=True, text='至少要有一位角色在回合中')
    elif method == 'round:finish':
        if not gm:
            raise NotGm()
        query.edit_message_text('回合轮已结束')
        remove_round(bot, game_round.chat_id)
    return


def remove_round(bot: telegram.Bot, chat_id):
    for game_round in Round.objects.filter(chat_id=chat_id).all():
        message_id = game_round.message_id
        game_round.delete()
        try:
            bot.delete_message(chat_id, message_id)
        except TelegramError:
            continue


def refresh_round_message(bot: telegram.Bot, game_round: Round, refresh=False):
    actors = game_round.get_actors()
    if not actors:
        return
    game_round.counter = game_round.counter % len(actors)
    counter = game_round.counter
    state = ''
    if game_round.hide:
        state = '[隐藏列表]'
    text = '<b>回合指示器</b> {state} #round\n\n第 {round_number} 轮   [{counter}/{total}]\n\n'.format(
        state=state,
        round_number=game_round.round_counter,
        counter=counter+1,
        total=len(actors),
    )
    for index, actor in enumerate(actors):
        is_current = counter == index
        if is_current:
            text += '• {} ({}) ← 当前\n'.format(actor.name, actor.value)
        elif not game_round.hide:
            text += '◦ {} ({})\n'.format(actor.name, actor.value)

    if refresh:
        try:
            bot.edit_message_text(
                text,
                chat_id=game_round.chat_id,
                message_id=game_round.message_id,
                parse_mode='HTML',
                reply_markup=ROUND_REPLY_MARKUP,
            )
        except TelegramError:
            pass
    else:
        bot.delete_message(game_round.chat_id, game_round.message_id)
        message = bot.send_message(game_round.chat_id, text, parse_mode='HTML', reply_markup=ROUND_REPLY_MARKUP)
        game_round.message_id = message.message_id
        game_round.save()


def start_round(bot: telegram.Bot, update: telegram.Update, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if not is_valid_chat_type(message.chat):
        return error_message(message, job_queue, '必须在群聊中使用此命令')
    chat = message.chat
    text = '回合指示器 #round\n\n\n' \
           '没有人加入回合，使用 <code>.init [值]</code> 来加入回合\n\n' \
           '可以用 /hide 将回合列表转为隐藏式'
    message = chat.send_message(text, parse_mode='HTML')
    message_id = message.message_id
    chat_id = message.chat_id
    remove_round(bot, chat_id)
    Round.objects.create(chat_id=chat_id, message_id=message_id, hide=False)


def get_round(update: telegram.Update, job_queue) -> Optional[Round]:
    message = update.message
    assert isinstance(message, telegram.Message)
    if not is_valid_chat_type(message.chat):
        return error_message(message, job_queue, '必须在群聊中使用此命令')
    game_round = Round.objects.filter(chat_id=message.chat_id).first()
    if not game_round:
        return error_message(message, job_queue, '没有启动回合轮指示器')
    return game_round


def hide_round(bot: telegram.Bot, update: telegram.Update, job_queue):
    game_round = get_round(update, job_queue)
    if not game_round:
        return
    if not is_gm(update.message.chat_id, update.message.from_user.id):
        return error_message(update.message, job_queue, '只有 GM 才能使用此命令')
    game_round.hide = True
    game_round.save()
    refresh_round_message(bot, game_round, refresh=True)
    bot.delete_message(update.message.chat_id, update.message.message_id)


def public_round(bot: telegram.Bot, update: telegram.Update, job_queue):
    game_round = get_round(update, job_queue)
    if not game_round:
        return
    if not is_gm(update.message.chat_id, update.message.from_user.id):
        return error_message(update.message, job_queue, '只有 GM 才能使用此命令')
    game_round.hide = False
    game_round.save()
    refresh_round_message(bot, game_round, refresh=True)
    bot.delete_message(update.message.chat_id, update.message.message_id)


def next_turn(bot: telegram.Bot, update: telegram.Update, job_queue):
    game_round = get_round(update, job_queue)
    if not game_round:
        return
    actors = game_round.get_actors()
    next_count = game_round.counter + 1
    if next_count >= len(actors):
        next_count = 0
        game_round.round_counter += 1
    game_round.counter = next_count
    game_round.save()
    refresh_round_message(bot, game_round, refresh=False)
    bot.delete_message(update.message.chat_id, update.message.message_id)


def create_player(bot: telegram.Bot, message: telegram.Message, character_name: str) -> Player:
    assert isinstance(message.from_user, telegram.User)
    administrators = bot.get_chat_administrators(message.chat_id, timeout=250)
    is_admin = False
    for admin in administrators:
        if message.from_user.id == admin.user.id:
            is_admin = True
            break
    defaults = dict(
        character_name=character_name,
        full_name=message.from_user.full_name,
        username=message.from_user.username or '',
        is_gm=is_admin,
    )
    player, created = Player.objects.update_or_create(
        defaults=defaults,
        chat_id=message.chat_id,
        user_id=message.from_user.id,
    )
    return player


def set_name(bot: telegram.Bot, update: telegram.Update, args, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if len(args) == 0:
        return error_message(message, job_queue, '请在 /name 后写下你的角色名')
    user = message.from_user
    assert isinstance(user, telegram.User)
    name = ' '.join(args).strip()
    player = create_player(bot, message, name)
    if player.is_gm:
        message.chat.send_message('<b>主持人</b> {} 已设为 {}'.format(user.full_name, name), parse_mode='HTML')
    else:
        message.chat.send_message('<b>玩家</b> {} 已设为 {}'.format(user.full_name, name), parse_mode='HTML')
    delete_message(message)


def get_name(message: telegram.Message, temp=False) -> Optional[str]:
    user_id = message.from_user.id
    player = Player.objects.filter(chat_id=message.chat_id, user_id=user_id).first()
    if not player:
        return None
    elif temp:
        return player.temp_character_name
    return player.character_name


def set_dice_face(_, update, args, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    chat = get_chat(message.chat)
    if len(args) != 1:
        return error_message(
            message,
            job_queue,
            '需要（且仅需要）指定骰子的默认面数，'
            '目前为 <b>{}</b>'.format(chat.default_dice_face)
        )
    try:
        face = int(args[0])
    except ValueError:
        error_message(message, job_queue, '面数只能是数字')
        return
    chat.default_dice_face = face
    chat.save()


@run_async
def inline_callback(bot, update):
    query = update.callback_query
    assert isinstance(query, telegram.CallbackQuery)
    data = query.data or ''
    gm = is_gm(query.message.chat_id, query.from_user.id)
    try:
        if data.startswith('round'):
            with transaction.atomic():
                round_inline_callback(bot, query, gm)
            return
    except NotGm:
        query.answer(show_alert=True, text='只能 GM (Admin) 才能这样操作哦', cache_time=0)
        return
    # hide roll
    if data.startswith('hide_roll'):
        if not gm:
            query.answer('暗骰只有 GM 才能查看', show_alert=True)
        result = redis.get(data)
        if result:
            data = pickle.loads(result)
            text = data['text'].replace('<code>', '').replace('</code>', '')
        else:
            text = '找不到这条暗骰记录'
        query.answer(
            show_alert=True,
            text=text,
            cache_time=10000,
        )
    else:
        query.answer(show_alert=True, text='未知指令')


def handle_coc_roll(
        message: telegram.Message, command: str,
        name: str, text: str, job_queue: JobQueue, **_):
    """
    Call of Cthulhu
    """
    hide = command.find('h') != -1
    text = text.strip()
    numbers = re.findall(r'\d{1,2}', text)
    if len(numbers) == 0:
        return error_message(message, job_queue, '格式错误。需要写技能值。')

    rolled_list = [secrets.randbelow(100) + 1]
    rolled = rolled_list[0]
    modification = ''
    skill_number = int(numbers[0])
    modifier_matched = re.search('[+-]', command)
    if modifier_matched:
        modifier = modifier_matched.group(0)
        extra = 1
        if len(numbers) > 1:
            extra = int(numbers[0])
            skill_number = int(numbers[1])
        for _ in range(extra):
            rolled_list.append(secrets.randbelow(100) + 1)
        if modifier == '+':
            rolled = min(rolled_list)
            modification += '奖励骰:'
        elif modifier == '-':
            rolled = max(rolled_list)
            modification += '惩罚骰:'
        modification += '<code>[{}]</code> '.format(', '.join(map(str, rolled_list)))
    half_skill_number = skill_number >> 1
    skill_number_divide_5 = skill_number // 5
    if rolled == 1:
        remark = '大成功'
    elif rolled <= skill_number_divide_5:
        remark = '极难成功'
    elif rolled <= half_skill_number:
        remark = '困难成功'
    elif rolled <= skill_number:
        remark = '成功'
    elif rolled == 100:
        remark = '大失败'
    elif rolled >= 95 and skill_number < 50:
        remark = '大失败'
    else:
        remark = '失败'
    result_text = '{} → <code>{}</code> {}\n\n{}'.format(text, rolled, remark, modification)
    handle_roll(message, name, result_text, job_queue, hide)


LOOP_ROLL_REGEX = re.compile(r'^\s*(\d{1,2})\s*')


def handle_loop_roll(message: telegram.Message, command: str, name: str, text: str, job_queue: JobQueue, **_):
    """
    Tales from the Loop
    """
    hide = command[-1] == 'h'
    text = text.strip()
    roll_match = LOOP_ROLL_REGEX.match(text)
    if not roll_match:
        return error_message(message, job_queue, '格式错误。需要 <code>.loop [个数，最多两位数] [可选的描述]</code>')
    number = int(roll_match.group(1))
    if number == 0:
        return error_message(message, job_queue, '错误，不能 roll 0 个骰子')
    counter = 0
    result_list = []
    for _ in range(number):
        result = secrets.randbelow(6) + 1
        result_list.append(str(result))
        if result == 6:
            counter += 1
    description = text[roll_match.end():]
    result_text = '<code>({}/{}) [{}]</code> {}'.format(counter, number, ', '.join(result_list), description)
    handle_roll(message, name, result_text, job_queue, hide)


def handle_normal_roll(message: telegram.Message, command: str, name: str, start: int, job_queue: JobQueue, chat, **_):
    text = message_text_convert(message)
    text = text[start:].strip()
    hide = command[-1] == 'h'
    if text.strip() == '':
        text = 'd'
    try:
        _, result_text = dice.roll(text, chat.default_dice_face)
    except dice.RollError as e:
        return error_message(message, job_queue, e.args[0])
    handle_roll(message, name, result_text, job_queue, hide)


def handle_roll(message: telegram.Message, name: str, result_text: str, job_queue: JobQueue, hide=False):
    kind = LogKind.ROLL.value
    if hide:
        roll_id = str(uuid.uuid4())
        key = 'hide_roll:{}'.format(roll_id)
        redis.set(key, pickle.dumps({
            'text': result_text,
            'chat_id': message.chat_id,
        }))
        keyboard = [[InlineKeyboardButton("GM 查看", callback_data=key)]]

        reply_markup = InlineKeyboardMarkup(keyboard)
        text = '<b>{}</b> 投了一个隐形骰子'.format(name)
        kind = LogKind.HIDE_DICE.value
    else:
        text = '{} 🎲 {}'.format(name, result_text)
        reply_markup = None
    chat = get_chat(message.chat)
    if not chat.recording:
        text = '[未记录] ' + text
    sent = message.chat.send_message(
        text,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    user = message.from_user
    assert isinstance(user, telegram.User)
    if chat.recording:
        Log.objects.create(
            user_id=user.id,
            message_id=sent.message_id,
            chat=chat,
            content=result_text,
            user_fullname=user.full_name,
            character_name=name,
            gm=is_gm(message.chat_id, user.id),
            kind=kind,
            created=message.date,
        )
    context = dict(
        chat_id=message.chat_id,
        message_id_list=[message.message_id]
    )
    job_queue.run_once(delay_delete_messages, 10, context)


ME_REGEX = re.compile(r'^[.。]me\b|\s[.。]me\s?')
USERNAME_REGEX = re.compile(r'@([a-zA-Z0-9_]{5,})')


def is_author(message_id, user_id):
    return bool(Log.objects.filter(message_id=message_id, user_id=user_id).first())


def get_name_by_username(chat_id, username):
    player = Player.objects.filter(chat_id=chat_id, username=username).first()
    if not player:
        return None
    return player.character_name


def delay_delete_messages(bot: telegram.Bot, job):
    chat_id = job.context['chat_id']
    for message_id in job.context['message_id_list']:
        try:
            bot.delete_message(chat_id, message_id)
        except TelegramError:
            pass


def error_message(message: telegram.Message, job_queue: JobQueue, text: str):
    delete_time = 15
    try:
        sent = message.reply_text('<b>[ERROR]</b> {}'.format(text), parse_mode='HTML')
    except TelegramError:
        return
    context = dict(
        chat_id=message.chat_id,
        message_id_list=(message.message_id, sent.message_id),
    )
    job_queue.run_once(delay_delete_messages, delete_time, context=context)


def get_symbol(chat_id, user_id) -> str:
    symbol = ''
    if is_gm(chat_id, user_id):
        symbol = GM_SYMBOL
    return symbol + ' '


def is_empty_message(text):
    return ME_REGEX.sub('', text).strip() == ''


def message_text_convert(message: telegram.Message) -> str:
    if not message.text:
        return ''
    assert isinstance(message.text, str)
    last_index = 0
    segments = []

    def push_name(pushed_player: Player):
        segments.append('<b>{}</b>'.format(pushed_player.character_name))

    for entity in message.entities:
        assert isinstance(entity, telegram.MessageEntity)
        entity_offset = entity.offset
        entity_length = entity.length
        entity_end = entity_offset + entity_length
        if entity.type == entity.MENTION:
            segments.append(message.text[last_index:entity_offset])
            mention = message.text[entity_offset:entity_end]
            username = mention[1:]  # skip @
            player = Player.objects.filter(chat_id=message.chat_id, username=username).first()
            push_name(player)
            last_index = entity_end
        elif entity.type == entity.TEXT_MENTION:
            player = Player.objects.filter(chat_id=message.chat_id, user_id=entity.user.id).first()
            if not player:
                continue
            segments.append(message.text[last_index:entity_offset])
            push_name(player)
            last_index = entity_end
    segments.append(message.text[last_index:])
    return ''.join(segments)


# ..(space)..[name];..(space)..
AS_PATTERN = re.compile(r'^\s*([^;]+)[;；]\s*')


def handle_as_say(bot: telegram.Bot, chat, job_queue, message: telegram.Message,
                  start: int, with_photo=None, **_):
    user_id = message.from_user.id
    text = message_text_convert(message)[start:]
    match = AS_PATTERN.match(text)
    if match:
        name = match.group(1).strip()
        if name == '':
            return error_message(message, job_queue, '名字不能为空')
        set_temp_name(chat.chat_id, user_id, name)
        text = text[match.end():]
    if not is_gm(chat.chat_id, user_id):
        return error_message(message, job_queue, '.as 命令只有 GM 能用')
    else:
        name = get_temp_name(chat.chat_id, user_id) or ''
        if name == '':
            error_text = '''.as 的用法是 .as [名字]; [内容]。
如果之前用过 .as 的话可以省略名字的部分，直接写 .as [内容]。
但你之前并没有用过 .as'''
            return error_message(message, job_queue, error_text)

    handle_say(bot, chat, job_queue, message, name, text, with_photo=with_photo)


def handle_say(bot: telegram.Bot, chat, job_queue, message: telegram.Message,
               name: str, text: str, edit_log=None, with_photo=None):
    user_id = message.from_user.id
    gm = is_gm(message.chat_id, user_id)
    text = text.strip()
    if text.startswith('me'):
        text = '.' + text

    kind = LogKind.NORMAL.value

    if is_empty_message(text) and not with_photo:
        error_message(message, job_queue, '不能有空消息')
        return
    elif ME_REGEX.search(text):
        send_text = ME_REGEX.sub('<b>{}</b>'.format(name), text)
        content = send_text
        kind = LogKind.ME.value
    else:
        send_text = '<b>{}</b>: {}'.format(name, text)
        content = text
    symbol = get_symbol(message.chat_id, user_id)
    send_text = symbol + send_text
    # on edit
    if edit_log:
        assert isinstance(edit_log, Log)
        edit_log.content = content
        edit_log.kind = kind
        edit_log.save()
        bot.edit_message_text(send_text, message.chat_id, edit_log.message_id, parse_mode='HTML')
        delete_message(message)
        return

    # send message or photo
    reply_to_message_id = None
    reply_log = None
    target = message.reply_to_message
    if isinstance(target, telegram.Message) and target.from_user.id == bot.id:
        reply_to_message_id = target.message_id
        reply_log = Log.objects.filter(chat=chat, message_id=reply_to_message_id).first()
    if isinstance(with_photo, telegram.PhotoSize):
        sent = message.chat.send_photo(
            photo=with_photo,
            caption=send_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode='HTML',
        )
    else:
        if not chat.recording:
            send_text = '[未记录] ' + send_text
        sent = message.chat.send_message(
            send_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode='HTML',
        )

    if chat.recording:
        # record log
        created_log = Log.objects.create(
            message_id=sent.message_id,
            chat=chat,
            user_id=user_id,
            user_fullname=message.from_user.full_name,
            kind=kind,
            reply=reply_log,
            character_name=name,
            content=content,
            gm=gm,
            created=message.date,
        )
        # download and write photo file
        if isinstance(with_photo, telegram.PhotoSize):
            created_log.media.save('{}.jpeg'.format(uuid.uuid4()), io.BytesIO(b''))
            media = created_log.media.open('rb+')
            with_photo.get_file().download(out=media)
            media.close()
    delete_message(message)


def handle_delete(chat, message: telegram.Message, job_queue):
    target = message.reply_to_message
    if isinstance(target, telegram.Message):
        user_id = message.from_user.id
        log = Log.objects.filter(chat=chat, message_id=target.message_id).first()
        if log is None:
            error_message(message, job_queue, '这条记录不存在于数据库')
        elif log.user_id == user_id or is_gm(message.chat_id, user_id):
            delete_message(target)
            delete_message(message)
            log.deleted = True
            log.save()
        else:
            error_message(message, job_queue, '你没有删掉这条记录的权限')
    else:
        error_message(message, job_queue, '回复需要删除的记录')


def handle_replace(bot, chat, job_queue, message: telegram.Message, start: int):
    target = message.reply_to_message
    text = message.text[start:].strip()
    if not isinstance(target, telegram.Message):
        return error_message(message, job_queue, '回复需要编辑的记录')
    try:
        [old, new] = filter(lambda x: x != '', text.split('/'))
    except ValueError:
        return error_message(message, job_queue, '请用<code>/</code>分开需要替换的两部分，如 <code>苹果/香蕉</code>')
    assert isinstance(message.from_user, telegram.User)
    user_id = message.from_user.id
    log = Log.objects.filter(chat=chat, message_id=target.message_id).first()
    text = log.content.replace(old, new)
    if log is None:
        error_message(message, job_queue, '找不到对应的消息')
    elif log.user_id == user_id:
        handle_say(bot, chat, job_queue, message, log.character_name, text, edit_log=log)
        delete_message(message)
    else:
        error_message(message, job_queue, '你没有编辑这条消息的权限')


def handle_edit(bot, chat, job_queue, message: telegram.Message, start: int):
    target = message.reply_to_message
    if not isinstance(target, telegram.Message):
        return error_message(message, job_queue, '回复需要编辑的记录')

    assert isinstance(message.from_user, telegram.User)
    user_id = message.from_user.id
    log = Log.objects.filter(chat=chat, message_id=target.message_id).first()
    if log is None:
        error_message(message, job_queue, '找不到对应的消息')
    elif log.user_id == user_id:
        text = message_text_convert(message)
        handle_say(bot, chat, job_queue, message, log.character_name, text[start:], edit_log=log)
        delete_message(message)
    else:
        error_message(message, job_queue, '你没有编辑这条消息的权限')


INITIATIVE_REGEX = re.compile(r'^(.+)=\s*(\d{1,4})$')


def handle_initiative(message: telegram.Message, job_queue, name: str, text: str, **_):
    text = text.strip()
    match = INITIATIVE_REGEX.match(text)
    number = text
    if match is not None:
        name = match.group(1).strip()
        number = match.group(2)
    elif not text.isnumeric() or len(text) > 4:
        usage = '用法： <code>.init [数字]</code> 或 <code>.init [角色名] = [数字]</code>'
        error_message(message, job_queue, usage)
        return

    game_round = Round.objects.filter(chat_id=message.chat_id).first()
    if not isinstance(game_round, Round):
        error_message(message, job_queue, '请先用 /round 指令开启回合轮')
    Actor.objects.create(belong_id=message.chat_id, name=name, value=int(number))
    refresh_round_message(message.bot, game_round, refresh=True)
    delete_message(message)


def handle_lift(message: telegram.Message, job_queue, chat: Chat):
    assert isinstance(message, telegram.Message)
    reply_to = message.reply_to_message
    user_id = message.from_user.id
    if not isinstance(reply_to, telegram.Message):
        return error_message(message, job_queue, '需要回复一条消息来转换')
    elif reply_to.from_user.id == message.bot.id:
        return error_message(message, job_queue, '需要回复一条玩家发送的消息')
    elif reply_to.from_user.id != user_id and not is_gm(message.chat_id, user_id):
        return error_message(message, job_queue, '你只能转换自己的消息，GM 能转换任何人的消息')
    name = get_name(reply_to)
    text = message_text_convert(reply_to)
    with_photo = handle_photo(reply_to)
    handle_say(message.bot, chat, job_queue, reply_to, name, text, with_photo=with_photo)
    delete_message(reply_to)
    delete_message(message)


def is_gm(chat_id: int, user_id: int) -> bool:
    player = Player.objects.filter(chat_id=chat_id, user_id=user_id).first()
    if not player:
        return False
    return player.is_gm


def update_player(chat_id, user: telegram.User):
    player = Player.objects.filter(chat_id=chat_id, user_id=user.id).first()
    if not player:
        return
    player.username = user.username or ''
    player.full_name = user.full_name
    player.save()


@run_async
def run_chat_job(_bot, update: telegram.Update):
    message = update.message
    assert isinstance(message, telegram.Message)
    update_player(message.chat_id, message.from_user)


def split(pattern, text):
    result = re.match(pattern, text)
    if result is None:
        return None
    else:
        command = result.group(1)
        return command, result.end()


@run_async
def handle_message(bot, update, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    with_photo = handle_photo(message)
    if not message.text or not message.text.startswith(('.', '。')):
        return
    if not is_valid_chat_type(message.chat):
        message.reply_text('只能在群中使用我哦')
        return
    elif not isinstance(message.from_user, telegram.User):
        return
    chat = get_chat(message.chat)
    name = get_name(message)
    if not name:
        error_message(message, job_queue, '请先使用 <code>/name [你的角色名]</code> 设置角色名')
        return

    handlers = [
        (re.compile(r'^[.。](rh?)\b'), handle_normal_roll),
        (re.compile(r'^[.。](loh?)\b'), handle_loop_roll),
        (re.compile(r'^[.。](coch?[+\-]?h?)\s*'), handle_coc_roll),
        (re.compile(r'^[.。](init)\b'), handle_initiative),
        (re.compile(r'^[.。](as)\b'), handle_as_say),
    ]

    for pattern, handler in handlers:
        result = split(pattern, message.text)
        if not result:
            continue
        command, start = result
        text = message.text[start:]
        handler(
            bot=bot,
            chat=chat,
            command=command,
            start=start,
            name=name,
            text=text,
            message=message,
            job_queue=job_queue,
            with_photo=with_photo,
        )
        return

    edit_command_matched = re.compile(r'^[.。](del|edit|lift|s)\b').match(message.text)
    if edit_command_matched:
        command = edit_command_matched.group(1)
        reply_to = message.reply_to_message
        if not chat.recording:
            error_message(message, job_queue, '未在记录中，无法编辑消息')
        elif not isinstance(reply_to, telegram.Message):
            error_message(message, job_queue, '先需要回复一则消息')
        elif command == 'lift':
            handle_lift(message, job_queue, chat)
        elif reply_to.from_user.id != bot.id:
            error_message(message, job_queue, '请回复 bot 发出的消息')
        elif command == 'del':
            handle_delete(chat, message, job_queue)
        elif command == 'edit':
            handle_edit(bot, chat, job_queue, message, start=edit_command_matched.end())
        elif command == 's':
            handle_replace(bot, chat, job_queue, message, start=edit_command_matched.end())
    else:
        handle_say(bot, chat, job_queue, message, name, message_text_convert(message)[1:], with_photo=with_photo)


def handle_photo(message: telegram.Message):
    photo_size_list = message.photo
    if len(photo_size_list) == 0:
        return None
    photo_size_list.sort(key=lambda p: p.file_size)
    return photo_size_list[-1]


def error(_, update, bot_error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, bot_error)


def handle_status(bot, update):
    assert isinstance(update.message, telegram.Message)
    message = update.message
    chat = get_chat(message.chat)
    if message.new_chat_title:
        chat.title = message.new_chat_title
        chat.save()
    if message.new_chat_members:
        for user in message.new_chat_members:
            if user.id == bot.id:
                message.chat.send_message(
                    START_TEXT,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )


def get_chat(telegram_chat: telegram.Chat) -> Chat:
    chat = Chat.objects.filter(
        chat_id=telegram_chat.id
    ).first()
    if chat:
        return chat
    else:
        return Chat.objects.create(
            chat_id=telegram_chat.id,
            title=telegram_chat.title,
        )


def set_password(_, update, args, job_queue):
    message = update.message
    assert isinstance(message, telegram.Message)
    if len(args) != 1:
        text = '输入 /password [你的密码] 设置密码。密码中不能有空格。'
        return error_message(message, job_queue, text)
    chat = get_chat(message.chat)
    chat.password = sha256(str(args[0]).encode()).hexdigest()
    chat.save()
    message.reply_text('密码已设置')


def main():
    """Start the bot."""
    # Create the EventHandler and pass it your bot's token.
    updater = Updater(TOKEN)

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    # on different commands - answer in Telegram
    dp.add_handler(CommandHandler("start", start_command, pass_job_queue=True))
    dp.add_handler(CommandHandler("save", save, pass_job_queue=True))
    dp.add_handler(CommandHandler("help", bot_help))
    dp.add_handler(CommandHandler('face', set_dice_face, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('name', set_name, pass_args=True, pass_job_queue=True))
    dp.add_handler(CommandHandler('round', start_round, pass_job_queue=True))
    dp.add_handler(CommandHandler('public', public_round, pass_job_queue=True))
    dp.add_handler(CommandHandler('hide', hide_round, pass_job_queue=True))
    dp.add_handler(CommandHandler('next', next_turn, pass_job_queue=True))
    dp.add_handler(CommandHandler('password', set_password, pass_args=True, pass_job_queue=True))

    dp.add_handler(MessageHandler(
        Filters.text | Filters.photo,
        handle_message,
        channel_post_updates=False,
        pass_job_queue=True,
    ))
    dp.add_handler(MessageHandler(Filters.status_update, handle_status))
    # always execute `run_chat_job`.
    dp.add_handler(
        MessageHandler(
            Filters.all,
            run_chat_job,
            channel_post_updates=False,
        ),
        group=42
    )

    updater.dispatcher.add_handler(CallbackQueryHandler(inline_callback))
    # log all errors
    dp.add_error_handler(error)

    # Start the Bot
    if 'WEBHOOK_URL' in os.environ:
        updater.start_webhook(listen='0.0.0.0', port=9990, url_path=TOKEN)
        url = os.path.join(os.environ['WEBHOOK_URL'], TOKEN)
        updater.bot.set_webhook(url=url)
    else:
        updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
