import streamlit as st
import re
import json
import time
import random
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Tuple

# --- Конфигурация страницы ---
st.set_page_config(
    page_title="🍎 Парсер отзывов App Store",
    page_icon="📱",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.title("📱 Парсер отзывов App Store")
st.caption("Инструмент собирает отзывы через официальное API Apple, фильтрует их и позволяет скачать в CSV.")


# --- Ядро логики (без сайд-эффектов) ---

@st.cache_resource
def setup_session() -> requests.Session:
    """Настраивает сессию с retry-механизмом и заголовками браузера."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'application/json',
        'Referer': 'https://apps.apple.com/'
    })
    return session


def extract_app_id(app_url: str) -> Optional[str]:
    match = re.search(r'/id(\d+)', app_url)
    if match:
        return match.group(1)
    return None


def parse_review_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if 'im:version' not in entry:
            return None

        author = entry.get('author', {}).get('name', {}).get('label', 'Аноним')
        title = entry.get('title', {}).get('label', '')
        content = entry.get('content', {}).get('label', '')
        if not content:
            content = entry.get('summary', {}).get('label', '')

        rating = int(entry.get('im:rating', {}).get('label', 0))
        version = entry.get('im:version', {}).get('label', 'N/A')

        raw_date = entry.get('im:date', {}).get('label', '')
        formatted_date = 'N/A'
        if raw_date:
            try:
                date_obj = datetime.strptime(raw_date[:19], '%Y-%m-%dT%H:%M:%S')
                formatted_date = date_obj.strftime('%d.%m.%Y')
            except ValueError:
                try:
                    date_obj = datetime.strptime(raw_date[:10], '%Y-%m-%d')
                    formatted_date = date_obj.strftime('%d.%m.%Y')
                except ValueError:
                    pass

        votes = int(entry.get('im:voteSum', {}).get('label', 0))
        review_url = entry.get('id', {}).get('label', '')

        return {
            'author': author,
            'title': title,
            'content': content,
            'rating': rating,
            'version': version,
            'date': formatted_date,
            'votes': votes,
            'review_url': review_url
        }
    except Exception as e:
        return None


@st.cache_data(show_spinner=False)
def fetch_reviews(app_id: str, target_limit: int = 50) -> List[Dict[str, Any]]:
    """Собирает отзывы через API. Кэшируется, чтобы не дергать API при смене сортировки."""
    session = setup_session()
    reviews = []
    seen_reviews: Set[Tuple[str, str, str]] = set()

    url = f"https://itunes.apple.com/ru/rss/customerreviews/id={app_id}/json"
    page = 1

    progress_text = st.empty()
    progress_bar = st.progress(0)

    while len(reviews) < target_limit:
        time.sleep(random.uniform(2.0, 4.0))
        params = {'page': page, 'l': 'ru'}

        try:
            response = session.get(url, params=params, timeout=15)
            if 'text/html' in response.headers.get('Content-Type', ''):
                st.error("Apple вернул HTML вместо JSON. Возможно, сработала защита от ботов.")
                break

            response.raise_for_status()
            data = response.json()

            entries = data.get('feed', {}).get('entry', [])
            if isinstance(entries, dict):
                entries = [entries]

            if not entries or len(entries) <= 1:
                break

            new_reviews_on_page = 0
            for entry in entries[1:]:
                if len(reviews) >= target_limit:
                    break
                parsed = parse_review_entry(entry)
                if not parsed:
                    continue

                text_for_check = (parsed['content'] + ' ' + parsed['title']).lower()
                if not re.search(r'[а-яё]', text_for_check):
                    continue

                review_key = (parsed['author'], parsed['content'], parsed['date'])
                if review_key in seen_reviews:
                    continue

                seen_reviews.add(review_key)
                reviews.append(parsed)
                new_reviews_on_page += 1

            if new_reviews_on_page == 0:
                break

            page += 1

            # Обновление прогресса
            progress = min(1.0, len(reviews) / target_limit)
            progress_bar.progress(progress)
            progress_text.text(f"Загружено {len(reviews)} из {target_limit} отзывов...")

        except requests.exceptions.RequestException:
            break
        except json.JSONDecodeError:
            break

    progress_text.empty()
    progress_bar.empty()
    return reviews


def filter_and_sort_reviews(reviews: List[Dict[str, Any]], sort_mode: str) -> List[Dict[str, Any]]:
    if not reviews:
        return []

    mode_map = {
        'newest': ('date', True),
        'useful': ('votes', True),
        'critical': ('rating', False),
        'positive': ('rating', True)
    }

    key, reverse = mode_map.get(sort_mode, ('date', True))

    if sort_mode == 'critical':
        filtered = [r for r in reviews if r['rating'] <= 2]
    elif sort_mode == 'positive':
        filtered = [r for r in reviews if r['rating'] >= 4]
    else:
        filtered = reviews

    return sorted(filtered, key=lambda x: x[key], reverse=reverse)[:30]


def prepare_csv_data(reviews: List[Dict[str, Any]]) -> pd.DataFrame:
    """Подготавливает DataFrame для выгрузки и отображения."""
    if not reviews:
        return pd.DataFrame()

    columns = ['author', 'title', 'content', 'rating', 'version', 'date', 'votes', 'review_url']
    df = pd.DataFrame(reviews)
    df = df[columns]

    # Очистка переносов строк для корректного отображения в DataFrame и CSV
    for col in ['author', 'title', 'content', 'version']:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace('\n', ' ').str.replace('\r', ' ')
    return df


# --- Интерфейс Streamlit ---

# Инициализация состояния
if 'app_id' not in st.session_state:
    st.session_state['app_id'] = None
if 'reviews' not in st.session_state:
    st.session_state['reviews'] = []
if 'sort_mode' not in st.session_state:
    st.session_state['sort_mode'] = 'newest'

with st.form("input_form"):
    app_url = st.text_input(
        "🔗 Ссылка на приложение в App Store",
        placeholder="https://apps.apple.com/ru/app/имя/id123456789"
    )

    col1, col2 = st.columns(2)
    with col1:
        submitted = st.form_submit_button("🚀 Собрать отзывы", type="primary")
    with col2:
        if st.session_state['reviews'] and st.form_submit_button("🔄 Очистить результаты"):
            st.session_state['reviews'] = []
            st.session_state['app_id'] = None
            st.rerun()

if submitted:
    app_id = extract_app_id(app_url)
    if not app_id:
        st.error("❌ Не удалось извлечь ID приложения из ссылки. Проверьте URL.")
    else:
        st.session_state['app_id'] = app_id
        with st.spinner(f"Сбор отзывов для ID: {app_id}..."):
            raw_reviews = fetch_reviews(app_id, target_limit=50)

        if not raw_reviews:
            st.warning("⚠️ Отзывы не найдены или произошла ошибка при сборе.")
        else:
            st.success(f"✅ Успешно собрано {len(raw_reviews)} отзывов.")
            st.session_state['reviews'] = raw_reviews
            st.rerun()

# Блок сортировки и отображения
if st.session_state['reviews']:
    st.divider()

    col_sort, _ = st.columns([1, 4])
    with col_sort:
        sort_choice = st.selectbox(
            "📊 Сортировка (применяется к уже собранным данным)",
            options=['newest', 'useful', 'critical', 'positive'],
            index=0,
            format_func=lambda x: {
                'newest': 'Сначала новые',
                'useful': 'Сначала полезные',
                'critical': 'Критика (1-2 звезды)',
                'positive': 'Позитив (4-5 звезд)'
            }.get(x, x)
        )

    if sort_choice != st.session_state['sort_mode']:
        st.session_state['sort_mode'] = sort_choice
        st.rerun()

    final_reviews = filter_and_sort_reviews(st.session_state['reviews'], st.session_state['sort_mode'])
    df_display = prepare_csv_data(final_reviews)

    if not df_display.empty:
        st.dataframe(
            df_display,
            use_container_width=True,
            height=600,
            column_config={
                "review_url": st.column_config.LinkColumn("Ссылка", display_text="Открыть")
            }
        )




def prepare_csv_data(reviews: List[Dict[str, Any]]) -> pd.DataFrame:
    """Подготавливает DataFrame для выгрузки и отображения."""
    if not reviews:
        return pd.DataFrame()

    columns = ['author', 'title', 'content', 'rating', 'version', 'date', 'votes', 'review_url']
    df = pd.DataFrame(reviews)
    df = df[columns]

    # Функция для исправления двойного кодирования (cp1251 -> utf-8)
    def fix_encoding(text: str) -> str:
        if not isinstance(text, str):
            return text
        try:
            # Если строка содержит "кракозябры", пробуем восстановить
            if any(ord(c) > 127 for c in text):
                return text.encode('cp1251', errors='ignore').decode('utf-8', errors='ignore')
        except Exception:
            pass
        return text

    # Применяем исправление ко всем текстовым колонкам
    for col in ['author', 'title', 'content', 'version']:
        if col in df.columns:
            # Сначала убираем переносы, потом чиним кодировку
            df[col] = df[col].astype(str).str.replace('\n', ' ').str.replace('\r', ' ')
            df[col] = df[col].apply(fix_encoding)
            
    return df

        





        
        # Подготовка CSV для скачивания
        csv_data = df_display.to_csv(index=False, encoding='utf-8-sig', sep=';')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"reviews_{st.session_state['app_id']}_{timestamp}.csv"

        st.download_button(
            label=f"💾 Скачать {len(final_reviews)} отзывов в CSV",
            data=csv_data,
            file_name=filename,
            mime="text/csv"
        )
