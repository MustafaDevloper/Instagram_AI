import time
import random
import json
import os
import sys
import sqlite3
import threading
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
import logging
from dataclasses import dataclass, asdict
import pickle

from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, ChallengeRequired, 
    PleaseWaitFewMinutes, ClientError,
    UserNotFound, TwoFactorRequired
)
from requests.exceptions import ReadTimeout, ConnectionError
import requests
from bs4 import BeautifulSoup
import pytz

# ==================== KONFİGÜRASYON ====================
class Config:
    """Uygulama konfigürasyonu"""
    INSTA_USER = "instagram-kullanıcı-adınız"
    INSTA_PASS = "şifreniz"
    
    # API Anahtarları (Opsiyonel)
    WEATHER_API_KEY = ""
    NEWS_API_KEY = ""
    TRANSLATE_API_KEY = ""
    
    # Database dosyası
    DB_FILE = "bot_database.db"
    SESSION_FILE = "insta_session.json"
    
    # Admin kullanıcı ID'leri
    ADMIN_IDS = [123456789]  # Instagram user_id'ler
    
    # Bot ayarları
    CHECK_INTERVAL = (25, 45)  # Mesaj kontrol aralığı (saniye)
    MAX_MESSAGE_LENGTH = 2000  # Instagram DM limiti
    MAX_RETRY_COUNT = 5
    
    # Güvenlik
    MAX_MESSAGES_PER_MINUTE = 10
    BLOCK_THRESHOLD = 100  # Spam için blok eşiği
    
    # Logging
    LOG_FILE = "bot.log"
    LOG_LEVEL = logging.INFO

# ==================== LOGGING ====================
def setup_logger():
    """Loglama sistemini kur"""
    logging.basicConfig(
        level=Config.LOG_LEVEL,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(Config.LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logger()

# ==================== VERİTABANI ====================
class Database:
    """SQLite veritabanı yönetimi"""
    
    def __init__(self):
        self.conn = sqlite3.connect(Config.DB_FILE, check_same_thread=False)
        self.create_tables()
    
    def create_tables(self):
        """Gerekli tabloları oluştur"""
        cursor = self.conn.cursor()
        
        # Kullanıcı istatistikleri
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                message_count INTEGER DEFAULT 0,
                fikra_count INTEGER DEFAULT 0,
                bilgi_count INTEGER DEFAULT 0,
                game_wins INTEGER DEFAULT 0,
                is_blocked BOOLEAN DEFAULT 0,
                settings TEXT DEFAULT '{}'
            )
        ''')
        
        # Mesaj logları
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                response TEXT,
                timestamp TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        
        # Oturum durumları
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                state_data TEXT,
                expires TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        ''')
        
        # API cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                expires TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Kullanıcı bilgilerini getir"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row:
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None
    
    def create_user(self, user_id: int, username: str = ""):
        """Yeni kullanıcı oluştur"""
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, now, now))
        self.conn.commit()
    
    def update_user_stats(self, user_id: int, field: str, increment: int = 1):
        """Kullanıcı istatistiklerini güncelle"""
        cursor = self.conn.cursor()
        cursor.execute(f'''
            UPDATE users 
            SET {field} = {field} + ?, last_seen = ?
            WHERE user_id = ?
        ''', (increment, datetime.now().isoformat(), user_id))
        self.conn.commit()
    
    def log_message(self, user_id: int, message: str, response: str):
        """Mesajı logla"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO messages (user_id, message, response, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (user_id, message, response, datetime.now().isoformat()))
        self.conn.commit()
    
    def set_session(self, user_id: int, state: str, data: Dict, ttl: int = 300):
        """Oturum durumunu kaydet"""
        cursor = self.conn.cursor()
        expires = (datetime.now() + timedelta(seconds=ttl)).isoformat()
        data_json = json.dumps(data, ensure_ascii=False)
        
        cursor.execute('''
            INSERT OR REPLACE INTO sessions (user_id, state, state_data, expires)
            VALUES (?, ?, ?, ?)
        ''', (user_id, state, data_json, expires))
        self.conn.commit()
    
    def get_session(self, user_id: int) -> Optional[Dict]:
        """Oturum durumunu getir"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT state, state_data FROM sessions 
            WHERE user_id = ? AND expires > ?
        ''', (user_id, datetime.now().isoformat()))
        
        row = cursor.fetchone()
        if row:
            return {
                'state': row[0],
                'data': json.loads(row[1]) if row[1] else {}
            }
        return None
    
    def clear_session(self, user_id: int):
        """Oturumu temizle"""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        self.conn.commit()

# ==================== VERİ YAPILARI ====================
@dataclass
class UserStats:
    """Kullanıcı istatistikleri"""
    user_id: int
    message_count: int = 0
    fikra_count: int = 0
    bilgi_count: int = 0
    game_wins: int = 0
    last_active: Optional[datetime] = None

@dataclass
class BotStats:
    """Bot istatistikleri"""
    start_time: datetime
    total_messages: int = 0
    total_users: int = 0
    uptime: timedelta = timedelta(0)

class CommandCategory(Enum):
    """Komut kategorileri"""
    WEATHER = "🌤️ Hava Durumu"
    FUN = "😂 Eğlence"
    KNOWLEDGE = "🧠 Bilgi"
    MOTIVATION = "💪 Motivasyon"
    FOOD = "🍽️ Yemek"
    NEWS = "📰 Haberler"
    GAMES = "🎲 Oyunlar"
    TIME = "🕒 Zaman"
    UTILITIES = "🛠️ Araçlar"
    ADMIN = "🔧 Yönetici"

# ==================== VERİ SAĞLAYICILAR ====================
class DataProvider:
    """Harici veri sağlayıcıları"""
    
    @staticmethod
    def get_weather(city: str) -> Optional[Dict]:
        """OpenWeatherMap API ile hava durumu"""
        if not Config.WEATHER_API_KEY:
            return None
        
        try:
            url = f"http://api.openweathermap.org/data/2.5/weather"
            params = {
                'q': city,
                'appid': Config.WEATHER_API_KEY,
                'units': 'metric',
                'lang': 'tr'
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return {
                    'city': data['name'],
                    'temp': data['main']['temp'],
                    'feels_like': data['main']['feels_like'],
                    'humidity': data['main']['humidity'],
                    'description': data['weather'][0]['description'],
                    'wind_speed': data['wind']['speed'],
                    'icon': data['weather'][0]['icon']
                }
        except Exception as e:
            logger.error(f"Weather API error: {e}")
        
        return None
    
    @staticmethod
    def get_news() -> List[Dict]:
        """Haberleri getir"""
        news_items = []
        
        try:
            # Türk haber sitelerinden RSS
            sources = [
                "https://www.bbc.com/turkce/topics/cjgn7n9zzq7t?page=1",
                "https://www.trthaber.com/manset_articles.rss"
            ]
            
            for source in sources:
                response = requests.get(source, timeout=10)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, 'xml')
                    items = soup.find_all('item')[:5]
                    
                    for item in items:
                        title = item.find('title').text
                        link = item.find('link').text
                        news_items.append({'title': title, 'link': link})
                        
        except Exception as e:
            logger.error(f"News fetch error: {e}")
        
        return news_items
    
    @staticmethod
    def get_exchange_rates() -> Dict:
        """Döviz kurlarını getir"""
        try:
            url = "https://api.exchangerate-api.com/v4/latest/TRY"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return {
                    'USD': data['rates']['USD'],
                    'EUR': data['rates']['EUR'],
                    'GBP': data['rates']['GBP']
                }
        except:
            pass
        
        # Fallback simüle data
        return {
            'USD': round(random.uniform(28.0, 32.0), 2),
            'EUR': round(random.uniform(30.0, 34.0), 2),
            'GBP': round(random.uniform(35.0, 38.0), 2)
        }

# ==================== İÇERİK YÖNETİMİ ====================
class ContentManager:
    """Bot içeriğini yönet"""
    
    FIKRALAR = [
        "Geçen gün bi taksi çevirdim hala dönüyor.",
        "Bi adam gülmüş karısı da papatya",
        "İki yanlış bir Wi-Fi bağlatmaz!",
        "Programcı hayatı: 99 başarısızlık, 1 çalışıyor. Çalışanı sil, 99'a geri dön.",
        "C++: İnsanın kendi ayağına sıkabileceği en güçlü silah."
    ]
    
    BILGILER = [
        "Zürafaların ses telleri yoktur.",
        "Bir insanın parmak izi gibi dil izi de benzersizdir.",
        "Bal bozulmayan tek gıdadır.",
        "Dünyadaki karıncaların toplam ağırlığı, insanların toplam ağırlığına eşittir.",
        "Bir insan hayatı boyunca ortalama 35 ton yemek yer.",
        "Uzayda ağlamak imkansızdır çünkü gözyaşları düşmez."
    ]
    
    SOZLER = [
        "Hayat bir hıyardır, tuzu olan koşsun.",
        "Azimle sıçan, duvarı deler.",
        "Bugünün işini yarına bırakma, yarın başka işin çıkar.",
        "Kod yazmak: %10 ilham, %90 stackoverflow."
    ]
    
    YEMEKLER = [
        {"name": "🌯 Dürüm", "desc": "Acılı, soğanlı, bol salatalı", "calories": 450},
        {"name": "🍕 Pizza", "desc": "Pepperoni, ekstra peynir", "calories": 850},
        {"name": "🥙 Lahmacun", "desc": "Bol limonlu, kıymalı", "calories": 300},
        {"name": "🍔 Burger", "desc": "Çift köfte, cheddar, bacon", "calories": 750},
        {"name": "🍝 Makarna", "desc": "Bolonez soslu", "calories": 500},
        {"name": "🍣 Sushi", "desc": "Somon, avokado", "calories": 350},
        {"name": "🥗 Salata", "desc": "Akdeniz usulü", "calories": 250}
    ]
    
    @staticmethod
    def get_random_fikra() -> str:
        return random.choice(ContentManager.FIKRALAR)
    
    @staticmethod
    def get_random_bilgi() -> str:
        return random.choice(ContentManager.BILGILER)
    
    @staticmethod
    def get_random_soz() -> str:
        return random.choice(ContentManager.SOZLER)
    
    @staticmethod
    def get_random_yemek() -> Dict:
        yemek = random.choice(ContentManager.YEMEKLER)
        yemek['price'] = random.randint(30, 150)
        yemek['rating'] = random.randint(7, 10) / 2  # 3.5-5.0 yıldız
        return yemek

# ==================== OYUN SİSTEMİ ====================
class GameEngine:
    """Oyun motoru"""
    
    class GameType(Enum):
        NUMBER_GUESS = "sayı_tahmin"
        ROCK_PAPER_SCISSORS = "tas_kagit_makas"
        QUIZ = "bilgi_yarismasi"
        LOTTERY = "sayisal_loto"
    
    def __init__(self, db: Database):
        self.db = db
        self.active_games = {}
    
    def start_number_game(self, user_id: int, min_num: int = 1, max_num: int = 100) -> str:
        """Sayı tahmin oyunu başlat"""
        target = random.randint(min_num, max_num)
        game_data = {
            'type': self.GameType.NUMBER_GUESS.value,
            'target': target,
            'min': min_num,
            'max': max_num,
            'attempts': 0,
            'max_attempts': 10,
            'start_time': datetime.now().isoformat()
        }
        
        self.active_games[user_id] = game_data
        self.db.set_session(user_id, 'game', game_data, ttl=600)
        
        return f"🎯 {min_num} ile {max_num} arasında bir sayı tuttum! 10 deneme hakkın var."
    
    def guess_number(self, user_id: int, guess: str) -> str:
        """Sayı tahmin et"""
        game = self.active_games.get(user_id)
        if not game or game['type'] != self.GameType.NUMBER_GUESS.value:
            return "Aktif bir tahmin oyunun yok."
        
        try:
            guess_num = int(guess)
        except ValueError:
            return "Lütfen geçerli bir sayı gir!"
        
        if guess_num < game['min'] or guess_num > game['max']:
            return f"Lütfen {game['min']}-{game['max']} arası bir sayı gir!"
        
        game['attempts'] += 1
        
        if guess_num < game['target']:
            status = "⬆️ Daha büyük bir sayı!"
        elif guess_num > game['target']:
            status = "⬇️ Daha küçük bir sayı!"
        else:
            # Kazandı
            del self.active_games[user_id]
            self.db.clear_session(user_id)
            self.db.update_user_stats(user_id, 'game_wins')
            
            return (
                f"🎉 TEBRİKLER! {game['attempts']} denemede bildin!\n"
                f"🏆 Kazandığın puan: {100 - game['attempts'] * 10}"
            )
        
        remaining = game['max_attempts'] - game['attempts']
        if remaining <= 0:
            del self.active_games[user_id]
            self.db.clear_session(user_id)
            return f"😔 Hakkın bitti! Sayı: {game['target']}"
        
        return f"{status} Kalan deneme: {remaining}"
    
    def rock_paper_scissors(self, player_choice: str) -> str:
        """Taş kağıt makas"""
        choices = {
            'taş': '🪨', 
            'kağıt': '📄', 
            'makas': '✂️'
        }
        
        player_choice = player_choice.lower()
        if player_choice not in choices:
            return "Geçerli bir seçim yap: taş, kağıt veya makas"
        
        bot_choice = random.choice(list(choices.keys()))
        
        # Kazananı belirle
        rules = {
            'taş': 'makas',
            'kağıt': 'taş',
            'makas': 'kağıt'
        }
        
        if player_choice == bot_choice:
            result = "🤝 BERABERE!"
        elif rules[player_choice] == bot_choice:
            result = "🎉 SEN KAZANDIN!"
        else:
            result = "😢 BEN KAZANDIM!"
        
        return (
            f"{choices[player_choice]} vs {choices[bot_choice]}\n\n"
            f"{result}"
        )
    
    def start_quiz(self, user_id: int) -> str:
        """Bilgi yarışması başlat"""
        questions = [
            {
                'question': "Türkiye'nin başkenti neresidir?",
                'options': ["İstanbul", "Ankara", "İzmir", "Bursa"],
                'answer': 1
            },
            {
                'question': "Güneş sistemindeki en büyük gezegen hangisidir?",
                'options': ["Dünya", "Mars", "Jüpiter", "Satürn"],
                'answer': 2
            },
            {
                'question': "İnsan vücudunda kaç kemik bulunur?",
                'options': ["106", "187", "206", "305"],
                'answer': 2
            }
        ]
        
        question = random.choice(questions)
        game_data = {
            'type': self.GameType.QUIZ.value,
            'question': question,
            'start_time': datetime.now().isoformat()
        }
        
        self.active_games[user_id] = game_data
        self.db.set_session(user_id, 'game', game_data, ttl=300)
        
        options_text = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(question['options'])])
        
        return (
            f"❓ Bilgi Yarışması!\n\n"
            f"{question['question']}\n\n"
            f"{options_text}\n\n"
            f"Cevap numarasını yaz!"
        )
    
    def check_quiz_answer(self, user_id: int, answer: str) -> str:
        """Quiz cevabını kontrol et"""
        game = self.active_games.get(user_id)
        if not game or game['type'] != self.GameType.QUIZ.value:
            return "Aktif bir quiz oyunun yok."
        
        try:
            answer_num = int(answer) - 1
            question = game['question']
            
            if answer_num == question['answer']:
                del self.active_games[user_id]
                self.db.clear_session(user_id)
                self.db.update_user_stats(user_id, 'game_wins')
                return "✅ Doğru cevap! 🏆"
            else:
                correct = question['options'][question['answer']]
                del self.active_games[user_id]
                self.db.clear_session(user_id)
                return f"❌ Yanlış cevap. Doğrusu: {correct}"
        except:
            return "Geçersiz cevap."

# ==================== GÜVENLİK SİSTEMİ ====================
class SecurityManager:
    """Güvenlik yönetimi"""
    
    def __init__(self, db: Database):
        self.db = db
        self.message_timestamps = {}
        self.spam_detection = {}
    
    def check_rate_limit(self, user_id: int) -> bool:
        """Rate limit kontrolü"""
        now = time.time()
        
        if user_id not in self.message_timestamps:
            self.message_timestamps[user_id] = []
        
        # 1 dakika içindeki mesajları temizle
        self.message_timestamps[user_id] = [
            ts for ts in self.message_timestamps[user_id] 
            if now - ts < 60
        ]
        
        # Limit kontrolü
        if len(self.message_timestamps[user_id]) >= Config.MAX_MESSAGES_PER_MINUTE:
            return False
        
        self.message_timestamps[user_id].append(now)
        return True
    
    def detect_spam(self, user_id: int, message: str) -> bool:
        """Spam tespiti"""
        # Basit spam tespiti
        spam_patterns = [
            r"(http|https)://",
            r"\.com|\.net|\.org",
            r"@\w+",
            r"[A-Z]{5,}",  # Çok fazla büyük harf
        ]
        
        for pattern in spam_patterns:
            if re.search(pattern, message, re.IGNORECASE):
                self.spam_detection[user_id] = self.spam_detection.get(user_id, 0) + 1
                
                if self.spam_detection[user_id] > Config.BLOCK_THRESHOLD:
                    self.block_user(user_id)
                    return True
        
        return False
    
    def block_user(self, user_id: int):
        """Kullanıcıyı engelle"""
        cursor = self.db.conn.cursor()
        cursor.execute('UPDATE users SET is_blocked = 1 WHERE user_id = ?', (user_id,))
        self.db.conn.commit()
        logger.warning(f"User {user_id} blocked for spam")
    
    def is_user_blocked(self, user_id: int) -> bool:
        """Kullanıcı engelli mi?"""
        user = self.db.get_user(user_id)
        return user and user['is_blocked'] == 1

# ==================== UTİLİTY FONKSİYONLAR ====================
class Utilities:
    """Yardımcı fonksiyonlar"""
    
    @staticmethod
    def format_time_delta(delta: timedelta) -> str:
        """Zaman farkını formatla"""
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        
        parts = []
        if days > 0:
            parts.append(f"{days} gün")
        if hours > 0:
            parts.append(f"{hours} saat")
        if minutes > 0:
            parts.append(f"{minutes} dakika")
        if seconds > 0 or not parts:
            parts.append(f"{seconds} saniye")
        
        return " ".join(parts)
    
    @staticmethod
    def add_city_suffix(city: str) -> str:
        """Şehir ismine -e hali ekle"""
        city = city.strip().title()
        
        special_cases = {
            'İstanbul': 'İstanbul\'a',
            'Ankara': 'Ankara\'ya',
            'İzmir': 'İzmir\'e',
            'Antalya': 'Antalya\'ya',
            'Bursa': 'Bursa\'ya',
            'Adana': 'Adana\'ya'
        }
        
        if city in special_cases:
            return special_cases[city]
        
        return f"{city}'e"
    
    @staticmethod
    def get_current_time() -> Dict:
        """Mevcut zaman bilgileri"""
        now = datetime.now()
        turkey_tz = pytz.timezone('Europe/Istanbul')
        now_tr = now.astimezone(turkey_tz)
        
        days = {
            'Monday': 'Pazartesi',
            'Tuesday': 'Salı',
            'Wednesday': 'Çarşamba',
            'Thursday': 'Perşembe',
            'Friday': 'Cuma',
            'Saturday': 'Cumartesi',
            'Sunday': 'Pazar'
        }
        
        return {
            'time': now_tr.strftime("%H:%M:%S"),
            'date': now_tr.strftime("%d/%m/%Y"),
            'day': days.get(now_tr.strftime("%A"), now_tr.strftime("%A")),
            'timezone': 'İstanbul (GMT+3)'
        }

# ==================== ANA BOT SINIFI ====================
class InstagramAIBot:
    """Ana bot sınıfı"""
    
    def __init__(self):
        self.client = Client()
        self.db = Database()
        self.security = SecurityManager(self.db)
        self.game_engine = GameEngine(self.db)
        self.data_provider = DataProvider()
        self.content_manager = ContentManager()
        self.utils = Utilities()
        
        self.bot_stats = BotStats(start_time=datetime.now())
        self.is_running = False
        
        # Komutlar
        self.commands = self._setup_commands()
        
        logger.info("Bot initialized")
    
    def _setup_commands(self) -> Dict:
        """Komut sistemi kurulumu"""
        return {
            'yardım': {
                'category': CommandCategory.UTILITIES,
                'description': 'Tüm komutları göster',
                'aliases': ['komutlar', 'help', 'menu']
            },
            'hava': {
                'category': CommandCategory.WEATHER,
                'description': 'Hava durumu bilgisi',
                'aliases': ['havadurumu', 'weather'],
                'usage': 'hava [şehir]'
            },
            'fıkra': {
                'category': CommandCategory.FUN,
                'description': 'Rastgele fıkra',
                'aliases': ['şaka', 'güldür', 'joke']
            },
            'bilgi': {
                'category': CommandCategory.KNOWLEDGE,
                'description': 'İlginç bilgi',
                'aliases': ['ilginç', 'fact', 'öğren']
            },
            'söz': {
                'category': CommandCategory.MOTIVATION,
                'description': 'Motivasyon sözü',
                'aliases': ['motivasyon', 'moral', 'quote']
            },
            'yemek': {
                'category': CommandCategory.FOOD,
                'description': 'Yemek önerisi',
                'aliases': ['neyesem', 'acıktım', 'food']
            },
            'haber': {
                'category': CommandCategory.NEWS,
                'description': 'Güncel haberler',
                'aliases': ['gündem', 'news']
            },
            'oyun': {
                'category': CommandCategory.GAMES,
                'description': 'Oyun menüsü',
                'aliases': ['games', 'play']
            },
            'saat': {
                'category': CommandCategory.TIME,
                'description': 'Saat ve tarih',
                'aliases': ['tarih', 'time', 'zaman']
            },
            'döviz': {
                'category': CommandCategory.UTILITIES,
                'description': 'Döviz kurları',
                'aliases': ['kur', 'exchange']
            },
            'istatistik': {
                'category': CommandCategory.UTILITIES,
                'description': 'Kişisel istatistikler',
                'aliases': ['stats', 'stat']
            },
            'bot': {
                'category': CommandCategory.UTILITIES,
                'description': 'Bot bilgisi',
                'aliases': ['botbilgi', 'info', 'hakkında']
            }
        }
    
    def login(self) -> bool:
        """Instagram'a giriş yap"""
        logger.info("Logging in to Instagram...")
        
        # Oturum dosyasını yükle
        if os.path.exists(Config.SESSION_FILE):
            try:
                self.client.load_settings(Config.SESSION_FILE)
                logger.info("Session loaded from file")
            except Exception as e:
                logger.error(f"Failed to load session: {e}")
        
        try:
            # Giriş yap
            login_result = self.client.login(Config.INSTA_USER, Config.INSTA_PASS)
            
            if login_result:
                # Oturumu kaydet
                self.client.dump_settings(Config.SESSION_FILE)
                logger.info("Login successful")
                return True
            else:
                logger.error("Login failed")
                return False
                
        except ChallengeRequired:
            logger.error("Challenge required. Manual intervention needed.")
            self._handle_challenge()
            return False
        except TwoFactorRequired:
            logger.error("Two-factor authentication required")
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False
    
    def _handle_challenge(self):
        """Challenge işlemi"""
        try:
            # Challenge kodu al
            challenge_info = self.client.get_challenge()
            if challenge_info:
                code = input("Enter challenge code sent to your email/phone: ")
                self.client.send_challenge_code(code)
                self.client.dump_settings(Config.SESSION_FILE)
                logger.info("Challenge completed")
        except Exception as e:
            logger.error(f"Challenge error: {e}")
    
    def process_message(self, user_id: int, username: str, message: str) -> Optional[str]:
        """Gelen mesajı işle"""
        # Kullanıcıyı veritabanına ekle
        self.db.create_user(user_id, username)
        
        # Rate limit kontrolü
        if not self.security.check_rate_limit(user_id):
            return "⏳ Çok hızlı mesaj gönderiyorsun. Lütfen 1 dakika bekleyin."
        
        # Spam kontrolü
        if self.security.detect_spam(user_id, message):
            return "🚫 Spam tespit edildi. Mesaj gönderimi engellendi."
        
        # Engelli kullanıcı kontrolü
        if self.security.is_user_blocked(user_id):
            return None
        
        # İstatistik güncelle
        self.db.update_user_stats(user_id, 'message_count')
        self.bot_stats.total_messages += 1
        
        # Mesajı küçük harfe çevir ve boşlukları temizle
        message_lower = message.lower().strip()
        
        # Oturum kontrolü
        session = self.db.get_session(user_id)
        if session:
            return self._handle_session(user_id, session, message_lower)
        
        # Komutları işle
        response = self._handle_command(user_id, message_lower)
        
        # Mesajı logla
        if response:
            self.db.log_message(user_id, message, response)
        
        return response
    
    def _handle_session(self, user_id: int, session: Dict, message: str) -> Optional[str]:
        """Oturum tabanlı işlemler"""
        session_type = session['state']
        
        if session_type == 'awaiting_city':
            self.db.clear_session(user_id)
            return self._get_weather_response(message)
        
        elif session_type == 'game':
            game_data = session['data']
            
            if game_data.get('type') == GameEngine.GameType.NUMBER_GUESS.value:
                return self.game_engine.guess_number(user_id, message)
            
            elif game_data.get('type') == GameEngine.GameType.QUIZ.value:
                return self.game_engine.check_quiz_answer(user_id, message)
        
        return None
    
    def _handle_command(self, user_id: int, message: str) -> Optional[str]:
        """Komutları işle"""
        # Özel durumlar
        if any(word in message for word in ['selam', 'merhaba', 'sa', 'hey', 'hi', 'hello']):
            return self._get_greeting_response()
        
        if any(word in message for word in ['nasılsın', 'naber', 'iyi misin']):
            return self._get_mood_response()
        
        if any(word in message for word in ['teşekkür', 'sağ ol', 'thanks']):
            return self._get_thank_you_response()
        
        # Komutları kontrol et
        for cmd, cmd_info in self.commands.items():
            if cmd in message or any(alias in message for alias in cmd_info['aliases']):
                return self._execute_command(user_id, cmd, message)
        
        # Bilinmeyen komut
        return self._get_unknown_command_response()
    
    def _execute_command(self, user_id: int, command: str, full_message: str) -> str:
        """Komutu çalıştır"""
        if command == 'yardım':
            return self._show_help()
        
        elif command == 'hava':
            return self._handle_weather_command(full_message, user_id)
        
        elif command == 'fıkra':
            self.db.update_user_stats(user_id, 'fikra_count')
            return f"😂 Fıkra:\n\n{self.content_manager.get_random_fikra()}"
        
        elif command == 'bilgi':
            self.db.update_user_stats(user_id, 'bilgi_count')
            return f"🧠 İlginç Bilgi:\n\n{self.content_manager.get_random_bilgi()}"
        
        elif command == 'söz':
            return f"💪 {self.content_manager.get_random_soz()}"
        
        elif command == 'yemek':
            yemek = self.content_manager.get_random_yemek()
            return (
                f"🍽️ Yemek Önerisi:\n\n"
                f"{yemek['name']}\n"
                f"{yemek['desc']}\n"
                f"⭐ {yemek['rating']:.1f}/5 | 💰 ~{yemek['price']} TL\n"
                f"🔥 {yemek['calories']} kalori"
            )
        
        elif command == 'haber':
            return self._get_news_response()
        
        elif command == 'oyun':
            return self._show_games_menu()
        
        elif command == 'saat':
            time_info = self.utils.get_current_time()
            return (
                f"🕒 Saat: {time_info['time']}\n"
                f"📅 Tarih: {time_info['date']}\n"
                f"📌 Gün: {time_info['day']}\n"
                f"🌍 Zaman Dilimi: {time_info['timezone']}"
            )
        
        elif command == 'döviz':
            return self._get_exchange_rates()
        
        elif command == 'istatistik':
            return self._get_user_stats(user_id)
        
        elif command == 'bot':
            return self._get_bot_info()
        
        return "Komut işlenemedi."
    
    def _handle_weather_command(self, message: str, user_id: int) -> str:
        """Hava durumu komutunu işle"""
        # Şehir adını çıkar
        city = None
        patterns = [
            r'hava\s+(durumu\s+)?(.+)',
            r'weather\s+(.+)$'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                city = match.group(2) if match.lastindex == 2 else match.group(1)
                break
        
        if city and city.strip():
            return self._get_weather_response(city.strip())
        else:
            # Şehir sor
            self.db.set_session(user_id, 'awaiting_city', {}, ttl=60)
            return "🌍 Hangi şehrin hava durumunu merak ediyorsun?"
    
    def _get_weather_response(self, city: str) -> str:
        """Hava durumu cevabı oluştur"""
        # API'den gerçek veri al
        real_weather = self.data_provider.get_weather(city)
        
        if real_weather:
            city_formatted = self.utils.add_city_suffix(city)
            emoji = self._get_weather_emoji(real_weather['icon'])
            
            return (
                f"{city_formatted} hava durumu:\n\n"
                f"🌡️ Sıcaklık: {real_weather['temp']}°C\n"
                f"🤔 Hissedilen: {real_weather['feels_like']}°C\n"
                f"🌤️ Durum: {real_weather['description'].title()} {emoji}\n"
                f"💧 Nem: %{real_weather['humidity']}\n"
                f"💨 Rüzgar: {real_weather['wind_speed']} m/s"
            )
        else:
            # Simüle edilmiş hava durumu
            temp = random.randint(-5, 35)
            conditions = [
                ("Güneşli", "☀️"), ("Parçalı Bulutlu", "⛅"), 
                ("Yağmurlu", "🌧️"), ("Karlı", "❄️"), 
                ("Rüzgarlı", "💨"), ("Sisli", "🌫️")
            ]
            condition, emoji = random.choice(conditions)
            city_formatted = self.utils.add_city_suffix(city)
            
            return (
                f"{city_formatted} hava durumu (simüle):\n\n"
                f"🌡️ Sıcaklık: {temp}°C\n"
                f"🌤️ Durum: {condition} {emoji}\n"
                f"💧 Nem: %{random.randint(30, 90)}\n"
                f"💨 Rüzgar: {random.randint(0, 15)} km/s"
            )
    
    def _get_news_response(self) -> str:
        """Haber cevabı oluştur"""
        news_items = self.data_provider.get_news()
        
        if news_items:
            news = random.choice(news_items[:5])
            return f"📰 Güncel Haber:\n\n{news['title']}\n\n🔗 {news['link']}"
        else:
            return "📰 Şu anda haber bulunamadı. Daha sonra tekrar deneyin."
    
    def _get_exchange_rates(self) -> str:
        """Döviz kurları cevabı oluştur"""
        rates = self.data_provider.get_exchange_rates()
        time_info = self.utils.get_current_time()
        
        response = "💱 Döviz Kurları:\n\n"
        for currency, rate in rates.items():
            response += f"{currency}: {rate:.2f} TL\n"
        
        response += f"\n📅 {time_info['date']} {time_info['time']}"
        return response
    
    def _show_games_menu(self) -> str:
        """Oyun menüsünü göster"""
        return (
            "🎮 OYUN MENÜSÜ 🎮\n\n"
            "1. 🎯 Sayı Tahmin Oyunu - 'sayı tahmin'\n"
            "2. 🪨📄✂️ Taş Kağıt Makas - 'tkm [seçimin]'\n"
            "3. ❓ Bilgi Yarışması - 'bilgi yarışması'\n"
            "4. 🎲 Zar At - 'zar at'\n"
            "5. 🪙 Yazı Tura - 'yazı tura'\n\n"
            "💡 Örnek: 'sayı tahmin' veya 'tkm taş'"
        )
    
    def _get_user_stats(self, user_id: int) -> str:
        """Kullanıcı istatistiklerini getir"""
        user = self.db.get_user(user_id)
        
        if not user:
            return "İstatistik bulunamadı."
        
        first_seen = datetime.fromisoformat(user['first_seen']).strftime('%d/%m/%Y %H:%M')
        last_seen = datetime.fromisoformat(user['last_seen']).strftime('%d/%m/%Y %H:%M')
        
        return (
            f"📊 {user['username'] or 'Kullanıcı'} İstatistikleri:\n\n"
            f"📅 İlk Görülme: {first_seen}\n"
            f"🕒 Son Görülme: {last_seen}\n"
            f"💬 Toplam Mesaj: {user['message_count']}\n"
            f"😂 Fıkra Dinleme: {user['fikra_count']}\n"
            f"🧠 Bilgi Öğrenme: {user['bilgi_count']}\n"
            f"🏆 Oyun Kazanma: {user['game_wins']}\n"
            f"👤 Kullanıcı ID: {user_id}"
        )
    
    def _get_bot_info(self) -> str:
        """Bot bilgilerini getir"""
        uptime = self.utils.format_time_delta(datetime.now() - self.bot_stats.start_time)
        
        return (
            "🤖 INSTAGRAM AI BOT v3.0\n\n"
            f"🚀 Çalışma Süresi: {uptime}\n"
            f"💬 Toplam Mesaj: {self.bot_stats.total_messages}\n"
            f"👥 Toplam Kullanıcı: {self.bot_stats.total_users}\n"
            f"📅 Başlangıç: {self.bot_stats.start_time.strftime('%d/%m/%Y %H:%M')}\n\n"
            "✨ Özellikler:\n"
            "• Akıllı komut sistemi\n"
            "• Gerçek hava durumu\n"
            "• Güncel haberler\n"
            "• Eğlenceli oyunlar\n"
            "• İstatistik takibi\n"
            "• Güvenlik sistemi\n\n"
            "🛠️ Geliştirici: @kullanici_adiniz\n"
            "🔒 Sürüm: 3.0.0 | Python 3.9+"
        )
    
    def _show_help(self) -> str:
        """Yardım mesajını göster"""
        help_text = "🤖 **ASİSTAN BOT KOMUTLARI** 🤖\n\n"
        
        # Komutları kategorilere göre grupla
        categories = {}
        for cmd, info in self.commands.items():
            category = info['category'].value
            if category not in categories:
                categories[category] = []
            
            aliases = '/'.join(info['aliases'][:2])
            desc = info['description']
            categories[category].append(f"• `{cmd}` ({aliases}) - {desc}")
        
        # Kategorileri ekle
        for category, commands in categories.items():
            help_text += f"{category}:\n" + "\n".join(commands) + "\n\n"
        
        help_text += (
            "🎮 **Oyun Komutları:**\n"
            "• `sayı tahmin` - Sayı tahmin oyunu\n"
            "• `tkm [taş/kağıt/makas]` - Taş kağıt makas\n"
            "• `bilgi yarışması` - Quiz oyunu\n"
            "• `zar at` - Zar atma\n"
            "• `yazı tura` - Yazı tura\n\n"
            "💡 *Örnek: 'hava İstanbul' veya 'fıkra'*"
        )
        
        return help_text
    
    def _get_greeting_response(self) -> str:
        """Selamlama cevabı"""
        greetings = [
            "Selam! 😊 Ben senin Instagram asistanınım.",
            "Merhaba! 🤖 Size nasıl yardımcı olabilirim?",
            "Hoş geldin! 🎉 Hadi sohbet edelim!",
            "Selamlar! ✨ Bugün nasılsın?"
        ]
        return f"{random.choice(greetings)}\n\nYardım için 'yardım' yazabilirsin."
    
    def _get_mood_response(self) -> str:
        """Durum cevabı"""
        moods = [
            "Harikayım! Seni görmek güzel 😊",
            "Kodlarım tıkırında, sen nasılsın? 🤖",
            "CPU'm %100, RAM'im dolu, hazırım! 💻",
            "Süperim! Yeni özellikler öğreniyorum 🚀",
            "Her zamankinden iyiyim! Sen? 🌟"
        ]
        return random.choice(moods)
    
    def _get_thank_you_response(self) -> str:
        """Teşekkür cevabı"""
        thanks = [
            "Rica ederim! 😊",
            "Her zaman yanındayım! 💙",
            "Benim için bir zevk! 🤖",
            "Sorun değil, başka ne yardımım olabilir?",
            "Yardımcı olabildiğime sevindim! ✨"
        ]
        return random.choice(thanks)
    
    def _get_unknown_command_response(self) -> str:
        """Bilinmeyen komut cevabı"""
        responses = [
            "Anlayamadım, yardım için 'yardım' yazabilirsin 🤔",
            "Bu komutu bilmiyorum, 'komutlar' yazarak neler yapabildiğimi görebilirsin 😊",
            "Üzgünüm, bunu henüz yapamıyorum. Komut listesi için 'yardım' yaz! 📝"
        ]
        return random.choice(responses)
    
    def _get_weather_emoji(self, icon_code: str) -> str:
        """Hava durumu ikonu için emoji"""
        icon_map = {
            '01': '☀️',  # açık
            '02': '⛅',  # az bulutlu
            '03': '☁️',  # parçalı bulutlu
            '04': '☁️',  # bulutlu
            '09': '🌧️',  # sağanak
            '10': '🌦️',  # yağmurlu
            '11': '⛈️',  # gök gürültülü
            '13': '❄️',  # kar
            '50': '🌫️'   # sis
        }
        
        code = icon_code[:2]
        return icon_map.get(code, '🌤️')
    
    def run(self):
        """Botu çalıştır"""
        if not self.login():
            logger.error("Login failed. Exiting.")
            return
        
        logger.info("Bot started successfully")
        self.is_running = True
        
        answered_messages = set()
        
        while self.is_running:
            try:
                # Rastgele bekleme
                sleep_time = random.uniform(*Config.CHECK_INTERVAL)
                logger.debug(f"Sleeping for {sleep_time:.1f} seconds")
                time.sleep(sleep_time)
                
                # Mesajları kontrol et
                threads = self.client.direct_threads(amount=20)
                
                for thread in threads:
                    if not thread.messages:
                        continue
                    
                    last_msg = thread.messages[0]
                    
                    # Botun kendi mesajını veya cevaplanan mesajı yoksay
                    if (last_msg.user_id == self.client.user_id or 
                        last_msg.id in answered_messages):
                        continue
                    
                    logger.info(f"New message from user {last_msg.user_id}: {last_msg.text[:5]}...")
                    
                    # Mesajı işle
                    response = self.process_message(
                        last_msg.user_id,
                        thread.users[0].username if thread.users else "Unknown",
                        last_msg.text
                    )
                    
                    # Cevap gönder
                    if response:
                        try:
                            # Mesajı parçalara böl (Instagram limiti)
                            max_len = Config.MAX_MESSAGE_LENGTH
                            if len(response) > max_len:
                                chunks = [response[i:i+max_len] for i in range(0, len(response), max_len)]
                                for chunk in chunks:
                                    self.client.direct_send(chunk, thread_ids=[thread.id])
                                    time.sleep(1)  # Rate limit için
                            else:
                                self.client.direct_send(response, thread_ids=[thread.id])
                            
                            answered_messages.add(last_msg.id)
                            logger.info(f"Response sent to user {last_msg.user_id}")
                            
                        except Exception as e:
                            logger.error(f"Failed to send message: {e}")
                
                # Cache temizle
                if len(answered_messages) > 1000:
                    answered_messages.clear()
                
                # İstatistik güncelle
                self.bot_stats.uptime = datetime.now() - self.bot_stats.start_time
                
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                self.is_running = False
                break
                
            except (ReadTimeout, ConnectionError) as e:
                logger.warning(f"Connection error: {e}. Retrying in 60 seconds...")
                time.sleep(60)
                
            except PleaseWaitFewMinutes as e:
                logger.warning(f"Instagram wait required: {e}. Waiting 5 minutes...")
                time.sleep(300)
                
            except ClientError as e:
                logger.error(f"Instagram client error: {e}")
                time.sleep(60)
                
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                time.sleep(60)
        
        logger.info("Bot stopped")

# ==================== ÇALIŞTIRMA ====================
if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════╗
    ║     INSTAGRAM AI BOT v1.0            ║
    ║     Developed with ❤️                ║
    ╚══════════════════════════════════════╝
    """)
    
    try:
        bot = InstagramAIBot()
        bot.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        print(f"Bot crashed: {e}")