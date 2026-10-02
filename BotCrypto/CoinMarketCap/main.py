import asyncio
import smtplib
import json
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import flet as ft


CMC_LISTING_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
REFRESH_SECONDS = 60
MAX_RESULTS = 1000
ALERT_THRESHOLD_WIDTH = 140
ALERT_CONTROL_HEIGHT = 48
ALERT_RESULTS_HEIGHT = 230
HISTORY_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "30m": 1800}
COLORS = {
    "background": "#050708",
    "surface": "#101719",
    "surface_alt": "#172225",
    "border": "#29383A",
    "text": "#E8F0EF",
    "muted": "#8FA3A2",
    "teal": "#42D6C4",
    "green": "#54D69A",
    "red": "#FF7B72",
    "orange": "#FFB45C",
}
WATCHLIST = {
    "BNB": {"BNB"},
    "Bitcoin": {"BTC"},
    "Dogecoin": {"DOGE"},
    "Litecoin": {"LTC"},
    "Polygon": {"POL", "MATIC"},
    "Solana": {"SOL"},
}


def fetch_market_data() -> list[dict]:
    query = urlencode(
        {
            "start": 1,
            "limit": MAX_RESULTS,
            "sortBy": "market_cap",
            "sortType": "desc",
            "convert": "USD",
            "cryptoType": "all",
            "tagType": "all",
            "audited": "false",
        }
    )
    request = Request(
        f"{CMC_LISTING_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/128.0",
        },
    )
    with urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)["data"]["cryptoCurrencyList"]


def quote_for(coin: dict) -> dict | None:
    return next(
        (quote for quote in coin.get("quotes", []) if quote.get("name") == "USD"),
        None,
    )


def market_history_key(coin: dict) -> str:
    return str(coin.get("id") or coin.get("symbol", "").upper())


def history_change(samples: list[tuple[float, float]], interval: str) -> float | None:
    interval_seconds = HISTORY_INTERVAL_SECONDS.get(interval)
    if interval_seconds is None or len(samples) < 2:
        return None
    current_time, current_price = samples[-1]
    cutoff = current_time - interval_seconds
    previous_price = next(
        (price for timestamp, price in reversed(samples[:-1]) if timestamp <= cutoff),
        None,
    )
    if previous_price is None or previous_price == 0:
        return None
    return ((current_price - previous_price) / previous_price) * 100


def format_price(value: float | None) -> str:
    if value is None:
        return "--"
    decimals = 2 if abs(value) >= 1 else 6
    formatted = f"{value:,.{decimals}f}"
    return "US$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def format_change(value: float | None) -> str:
    return "--" if value is None else f"{value:+.2f}%"


def send_alert_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=20) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_password)
            server.send_message(message)


def format_listing_date(coin: dict) -> tuple[str, str]:
    date_added = coin.get("dateAdded") or coin.get("date_added")
    if not date_added:
        return "--", "--"
    try:
        date = datetime.fromisoformat(str(date_added).replace("Z", "+00:00"))
    except ValueError:
        return "--", "--"
    return date.strftime("%m"), date.strftime("%Y")


def main(page: ft.Page) -> None:
    page.title = "Cotações | CoinMarketCap"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = COLORS["background"]
    page.padding = 4
    if not page.web:
        page.window.width = 1280
        page.window.height = 720
        page.window.min_width = 960
        page.window.min_height = 600

    fullscreen_state = False

    def table_frame_height() -> int:
        window_height = page.window.height or page.height or 720
        visible_rows = 10 if window_height >= 900 else 6
        return 46 + visible_rows * 50 + 32

    def table_width() -> int:
        window_width = page.window.width or 1280
        return max(860, round(window_width * 0.75 - 70))

    def apply_window_layout(is_fullscreen: bool) -> None:
        nonlocal fullscreen_state
        if is_fullscreen == fullscreen_state:
            return
        fullscreen_state = is_fullscreen
        if is_fullscreen:
            page.window.width = 1920
            page.window.height = 1080
            rows.width = table_width()
        else:
            page.window.width = 1280
            page.window.height = 720
            rows.width = table_width()
        table_frame.height = table_frame_height()
        rows.columns = build_columns()
        render_rows()

    def handle_window_event(event: ft.WindowEvent) -> None:
        if event.type in (ft.WindowEventType.ENTER_FULL_SCREEN, ft.WindowEventType.MAXIMIZE):
            apply_window_layout(True)
        elif event.type in (ft.WindowEventType.LEAVE_FULL_SCREEN, ft.WindowEventType.RESTORE):
            apply_window_layout(False)

    def handle_page_resize(event: ft.PageResizeEvent) -> None:
        rows.width = table_width()
        table_frame.height = table_frame_height()

    market_data: list[dict] = []
    price_history: dict[str, list[tuple[float, float]]] = {}
    selected_preferences = set(WATCHLIST)
    preference_options = dict(WATCHLIST)
    last_updated = ft.Text("Aguardando primeira atualização...", size=12, color=COLORS["muted"])
    status = ft.Text("Conectando ao CoinMarketCap...", size=12, color=COLORS["muted"])
    alert_threshold = ft.TextField(
        label="Queda alerta (%)",
        value="0.5",
        width=ALERT_THRESHOLD_WIDTH,
        height=ALERT_CONTROL_HEIGHT,
        text_size=11,
        dense=True,
        bgcolor=COLORS["surface_alt"],
        color=COLORS["text"],
        cursor_color=COLORS["orange"],
        border_color=COLORS["orange"],
        focused_border_color=COLORS["orange"],
        on_change=lambda event: refresh_monitor_alerts(),
    )
    positive_alert_threshold = ft.TextField(
        label="Alta alerta (%)",
        value="1.0",
        width=ALERT_THRESHOLD_WIDTH,
        height=ALERT_CONTROL_HEIGHT,
        text_size=11,
        dense=True,
        bgcolor=COLORS["surface_alt"],
        color=COLORS["text"],
        cursor_color=COLORS["green"],
        border_color=COLORS["green"],
        focused_border_color=COLORS["green"],
        on_change=lambda event: refresh_monitor_alerts(),
    )
    alert_intervals = ["1m", "5m", "30m", "1h", "24h"]
    negative_alert_intervals = {"1m"}
    positive_alert_intervals = {"1m"}

    def toggle_alert_interval(selected: set[str], interval: str, event: ft.ControlEvent) -> None:
        if event.control.value:
            selected.add(interval)
        else:
            selected.discard(interval)
        refresh_monitor_alerts()

    def build_interval_choices(selected: set[str], accent: str) -> ft.Row:
        return ft.Row(
            expand=True,
            height=24,
            scroll=ft.ScrollMode.HIDDEN,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Checkbox(
                    label=interval,
                    value=interval in selected,
                    active_color=accent,
                    check_color=COLORS["background"],
                    label_style=ft.TextStyle(size=10, color=COLORS["muted"]),
                    visual_density=ft.VisualDensity.COMPACT,
                    on_change=lambda event, choice=interval, selection=selected: toggle_alert_interval(
                        selection, choice, event
                    ),
                )
                for interval in alert_intervals
            ],
        )

    negative_interval_choices = build_interval_choices(negative_alert_intervals, COLORS["orange"])
    positive_interval_choices = build_interval_choices(positive_alert_intervals, COLORS["green"])
    monitor_alert_text = ft.Text(size=11, color=COLORS["red"], weight=ft.FontWeight.BOLD)
    monitor_alert = ft.Container(
        visible=False,
        padding=6,
        bgcolor="#351B1A",
        border=ft.Border.all(1, COLORS["red"]),
        border_radius=4,
        content=ft.Column(
            height=ALERT_RESULTS_HEIGHT,
            scroll=ft.ScrollMode.AUTO,
            controls=[monitor_alert_text],
        ),
    )
    positive_alert_text = ft.Text(size=11, color=COLORS["green"], weight=ft.FontWeight.BOLD)
    positive_monitor_alert = ft.Container(
        visible=False,
        padding=6,
        bgcolor="#153126",
        border=ft.Border.all(1, COLORS["green"]),
        border_radius=4,
        content=ft.Column(
            height=ALERT_RESULTS_HEIGHT,
            scroll=ft.ScrollMode.AUTO,
            controls=[positive_alert_text],
        ),
    )
    alert_results = ft.ResponsiveRow(
        columns=12,
        spacing=8,
        run_spacing=6,
        controls=[],
    )
    search = ft.TextField(
        hint_text="Buscar moeda",
        prefix_icon=ft.Icons.SEARCH,
        width=220,
        height=48,
        margin=ft.Margin.only(top=10),
        text_size=11,
        dense=True,
        bgcolor=COLORS["surface"],
        color=COLORS["text"],
        cursor_color=COLORS["teal"],
        border_color=COLORS["border"],
        focused_border_color=COLORS["teal"],
        on_change=lambda event: render_search_results(),
        on_submit=lambda event: add_preference_from_search(),
    )
    email_rules: list[dict] = []
    email_recipient = ft.TextField(label="Receber no e-mail", width=350, dense=True)
    email_coin = ft.TextField(label="Moeda (símbolo ou nome)", width=240, dense=True)
    email_direction = ft.Dropdown(
        label="Direção",
        value="Queda",
        width=150,
        dense=True,
        options=[ft.dropdown.Option("Queda"), ft.dropdown.Option("Alta")],
    )
    email_threshold = ft.TextField(label="Variação (%)", value="1.0", width=130, dense=True)
    email_interval = ft.Dropdown(
        label="Período",
        value="1m",
        width=130,
        dense=True,
        options=[ft.dropdown.Option(interval) for interval in alert_intervals],
    )
    smtp_host = ft.TextField(label="Servidor SMTP", width=350, dense=True, hint_text="smtp.gmail.com")
    smtp_port = ft.TextField(label="Porta", value="587", width=130, dense=True)
    smtp_user = ft.TextField(label="Usuário SMTP", width=300, dense=True)
    smtp_password = ft.TextField(
        label="Senha de app SMTP",
        width=300,
        dense=True,
        password=True,
        can_reveal_password=True,
    )
    smtp_sender = ft.TextField(label="E-mail remetente (opcional)", width=350, dense=True)
    email_alert_status = ft.Text(size=11, color=COLORS["muted"])
    email_rules_list = ft.Column(spacing=4, controls=[])

    def render_email_rules() -> None:
        email_rules_list.controls = [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(
                        f"{rule['coin']} | {rule['direction']} {rule['threshold']:g}% / {rule['interval']} → {rule['recipient']}",
                        size=11,
                        color=COLORS["text"],
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color=COLORS["muted"],
                        icon_size=16,
                        tooltip="Remover regra",
                        on_click=lambda event, selected_rule=rule: remove_email_rule(selected_rule),
                    ),
                ],
            )
            for rule in email_rules
        ]

    def remove_email_rule(rule: dict) -> None:
        email_rules.remove(rule)
        render_email_rules()
        email_alert_status.value = "Regra de e-mail removida."
        page.update()

    def add_email_rule(_: ft.ControlEvent | None = None) -> None:
        recipient = (email_recipient.value or "").strip()
        coin_query = (email_coin.value or "").strip()
        try:
            threshold_value = float((email_threshold.value or "").strip().replace(",", ".").replace("%", ""))
            port_value = int((smtp_port.value or "").strip())
            if threshold_value <= 0 or not 1 <= port_value <= 65535:
                raise ValueError
        except ValueError:
            email_alert_status.value = "Informe um percentual e uma porta válidos."
            page.update()
            return
        if "@" not in recipient or "." not in recipient.rsplit("@", 1)[-1]:
            email_alert_status.value = "Informe um endereço de e-mail válido."
            page.update()
            return
        host = (smtp_host.value or "").strip()
        username = (smtp_user.value or "").strip()
        password = smtp_password.value or ""
        if not all((host, username, password, coin_query)):
            email_alert_status.value = "Preencha moeda e os dados obrigatórios do servidor SMTP."
            page.update()
            return
        matching_coin = next(
            (
                coin
                for coin in market_data
                if coin_query.casefold() == coin.get("name", "").casefold()
                or coin_query.upper() == coin.get("symbol", "").upper()
            ),
            None,
        )
        if market_data and matching_coin is None:
            email_alert_status.value = "Moeda não encontrada na lista carregada. Use o símbolo, como BTC."
            page.update()
            return
        email_rules.append(
            {
                "coin": matching_coin.get("symbol", coin_query).upper() if matching_coin else coin_query.upper(),
                "recipient": recipient,
                "direction": email_direction.value or "Queda",
                "threshold": threshold_value,
                "interval": email_interval.value or "1m",
                "smtp_host": host,
                "smtp_port": port_value,
                "smtp_user": username,
                "smtp_password": password,
                "sender": (smtp_sender.value or "").strip() or username,
                "armed": True,
            }
        )
        render_email_rules()
        email_alert_status.value = "Alerta ativo. O envio acontece quando a variação atingir o limite."
        page.update()

    email_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Alertas por e-mail"),
        bgcolor=COLORS["surface"],
        content=ft.Container(
            width=680,
            content=ft.Column(
                height=560,
                scroll=ft.ScrollMode.AUTO,
                spacing=10,
                controls=[
                    ft.Text("Regra do alerta", size=12, color=COLORS["teal"], weight=ft.FontWeight.BOLD),
                    ft.ResponsiveRow(
                        columns=12,
                        spacing=8,
                        run_spacing=4,
                        controls=[
                            ft.Container(content=email_recipient, col={"xs": 12, "sm": 7}),
                            ft.Container(content=email_coin, col={"xs": 12, "sm": 5}),
                            ft.Container(content=email_direction, col={"xs": 12, "sm": 4}),
                            ft.Container(content=email_threshold, col={"xs": 6, "sm": 4}),
                            ft.Container(content=email_interval, col={"xs": 6, "sm": 4}),
                        ],
                    ),
                    ft.Divider(height=1, color=COLORS["border"]),
                    ft.Text("Servidor de envio SMTP", size=12, color=COLORS["teal"], weight=ft.FontWeight.BOLD),
                    ft.Text(
                        "Gmail: smtp.gmail.com, porta 587 e senha de app. Regras e credenciais ficam apenas na memória enquanto o app estiver aberto.",
                        size=11,
                        color=COLORS["muted"],
                    ),
                    ft.ResponsiveRow(
                        columns=12,
                        spacing=8,
                        run_spacing=4,
                        controls=[
                            ft.Container(content=smtp_host, col={"xs": 12, "sm": 8}),
                            ft.Container(content=smtp_port, col={"xs": 12, "sm": 4}),
                            ft.Container(content=smtp_user, col={"xs": 12, "sm": 6}),
                            ft.Container(content=smtp_password, col={"xs": 12, "sm": 6}),
                            ft.Container(content=smtp_sender, col={"xs": 12, "sm": 8}),
                        ],
                    ),
                    email_alert_status,
                    ft.Divider(height=1, color=COLORS["border"]),
                    ft.Text("Regras ativas", size=12, color=COLORS["teal"], weight=ft.FontWeight.BOLD),
                    email_rules_list,
                ],
            ),
        ),
        actions=[
            ft.TextButton("Fechar", on_click=lambda event: page.pop_dialog()),
            ft.FilledButton("Ativar alerta", icon=ft.Icons.ADD, on_click=add_email_rule),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def open_email_dialog(_: ft.ControlEvent | None = None) -> None:
        render_email_rules()
        page.show_dialog(email_dialog)

    def period_style(interval: str) -> tuple[str | None, str | None]:
        is_negative = interval in negative_alert_intervals
        is_positive = interval in positive_alert_intervals
        if is_negative and is_positive:
            return "#3A2B1C", COLORS["orange"]
        if is_negative:
            return "#351B1A", COLORS["red"]
        if is_positive:
            return "#153126", COLORS["green"]
        return None, None

    def period_column(interval: str, width: int) -> ft.DataColumn:
        background, accent = period_style(interval)
        return ft.DataColumn(
            ft.Container(
                width=width,
                bgcolor=background or COLORS["surface_alt"],
                padding=4,
                border_radius=4,
                content=ft.Text(
                    interval,
                    size=11 if len(interval) <= 3 else 12,
                    color=accent or COLORS["teal"],
                    weight=ft.FontWeight.BOLD,
                    text_align=ft.TextAlign.CENTER,
                ),
            )
        )

    def build_columns() -> list[ft.DataColumn]:
        columns = [
            ft.DataColumn(ft.Container(width=145, content=ft.Text("Criptomoeda", size=12, text_align=ft.TextAlign.CENTER))),
            ft.DataColumn(ft.Container(width=58, content=ft.Text("Símbolo", size=12, text_align=ft.TextAlign.CENTER))),
            ft.DataColumn(ft.Container(width=48, content=ft.Text("Ano", size=12, text_align=ft.TextAlign.CENTER))),
            ft.DataColumn(ft.Container(width=88, content=ft.Text("Preço", size=12, text_align=ft.TextAlign.CENTER))),
            ft.DataColumn(
                ft.Container(
                    content=ft.Text("Gráfico", size=12, color=COLORS["teal"], text_align=ft.TextAlign.CENTER),
                    alignment=ft.Alignment(0, 0),
                    bgcolor="#12332F",
                    padding=4,
                    width=82,
                    border_radius=4,
                )
            ),
        ]
        columns.extend(
            period_column(interval, 45)
            for interval in ("1m", "5m", "30m")
        )
        columns.extend(
            [
                period_column("1h", 52),
                period_column("24h", 52),
                ft.DataColumn(ft.Container(width=52, content=ft.Text("7d", size=12, text_align=ft.TextAlign.CENTER))),
                ft.DataColumn(ft.Container(width=52, content=ft.Text("30d", size=12, text_align=ft.TextAlign.CENTER))),
                ft.DataColumn(ft.Container(width=58, content=ft.Text("1 ano", size=11, text_align=ft.TextAlign.CENTER))),
            ]
        )
        return columns

    rows = ft.DataTable(
        expand=True,
        width=780,
        columns=build_columns(),
        rows=[],
        column_spacing=6,
        horizontal_margin=0,
        heading_row_height=46,
        data_row_min_height=46,
        data_row_max_height=50,
        bgcolor=COLORS["surface"],
        heading_row_color=COLORS["surface_alt"],
        heading_text_style=ft.TextStyle(color=COLORS["teal"], weight=ft.FontWeight.BOLD),
        data_text_style=ft.TextStyle(size=12, color=COLORS["text"]),
        horizontal_lines=ft.BorderSide(1, COLORS["border"]),
    )
    table_frame = ft.Container(
        height=table_frame_height(),
        bgcolor=COLORS["surface"],
        border=ft.Border.all(1, COLORS["border"]),
        border_radius=6,
        padding=12,
        content=ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                ft.Row(
                    expand=True,
                    scroll=ft.ScrollMode.AUTO,
                    controls=[rows],
                ),
            ],
        ),
    )
    conversation = ft.Column(
        expand=True,
        spacing=10,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Text(
                "Pergunte por uma moeda para ver preço e variações.",
                size=13,
                color=COLORS["text"],
            )
        ],
    )
    question = ft.TextField(
        hint_text="Moeda",
        expand=True,
        text_size=11,
        dense=True,
        bgcolor=COLORS["surface_alt"],
        color=COLORS["text"],
        cursor_color=COLORS["teal"],
        border_color=COLORS["border"],
        focused_border_color=COLORS["teal"],
        on_submit=lambda event: send_question(),
    )
    preference_buttons = ft.ResponsiveRow(columns=12, spacing=4, run_spacing=4)
    search_results = ft.Row(wrap=True, spacing=4, run_spacing=4)

    def add_message(text: str, from_user: bool) -> None:
        conversation.controls.append(
            ft.Container(
                padding=10,
                bgcolor="#12332F" if from_user else COLORS["surface_alt"],
                border_radius=8,
                content=ft.Text(text, size=13, color=COLORS["teal"] if from_user else COLORS["text"]),
            )
        )

    def answer_question(text: str) -> str:
        normalized = text.casefold()
        matches = [
            coin
            for coin in market_data
            if coin.get("name", "").casefold() in normalized
            or f" {coin.get('symbol', '').casefold()} " in f" {normalized} "
        ]
        if not matches:
            if not market_data:
                return "Ainda estou carregando as cotações. Tente novamente em instantes."
            return "Não encontrei essa moeda nos ativos carregados. Tente o nome completo ou o símbolo, como Solana ou SOL."

        coin = matches[0]
        quote = quote_for(coin) or {}
        return (
            f"{coin.get('name', '--')} ({coin.get('symbol', '--').upper()}): "
            f"{format_price(quote.get('price'))}. "
            f"1h {format_change(quote.get('percentChange1h'))}, "
            f"24h {format_change(quote.get('percentChange24h'))}, "
            f"7d {format_change(quote.get('percentChange7d'))}."
        )

    def send_question(_: ft.ControlEvent | None = None) -> None:
        text = question.value.strip() if question.value else ""
        if not text:
            return
        add_message(text, from_user=True)
        add_message(answer_question(text), from_user=False)
        question.value = ""
        page.update()

    def clear_question(_: ft.ControlEvent) -> None:
        question.value = ""
        conversation.controls = [
            ft.Text(
                "Pergunte por uma moeda para ver preço e variações.",
                size=13,
                color=COLORS["text"],
            )
        ]
        page.update()

    def add_preference_from_search() -> None:
        query = search.value.strip().casefold() if search.value else ""
        if not query:
            return
        match = next(
            (
                coin
                for coin in market_data
                if query in coin.get("name", "").casefold()
                or query == coin.get("symbol", "").casefold()
            ),
            None,
        )
        if match is None:
            status.value = "Moeda não encontrada. Digite o nome ou símbolo completo."
            page.update()
            return
        display_name = match.get("name", "").strip()
        symbol = match.get("symbol", "").upper()
        preference_options[display_name] = {symbol}
        selected_preferences.add(display_name)
        search.value = ""
        status.value = f"{display_name} adicionada às moedas preferidas."
        render_preference_buttons()
        render_rows()

    def add_preference(coin: dict) -> None:
        display_name = coin.get("name", "").strip()
        symbol = coin.get("symbol", "").upper()
        if not display_name or not symbol:
            return
        preference_options[display_name] = {symbol}
        selected_preferences.add(display_name)
        search.value = ""
        search_results.controls = []
        status.value = f"{display_name} adicionada às moedas preferidas."
        render_preference_buttons()
        render_rows()

    def render_search_results() -> None:
        query = search.value.strip().casefold() if search.value else ""
        if not query:
            search_results.controls = []
            page.update()
            return
        matches = [
            coin
            for coin in market_data
            if query in coin.get("name", "").casefold()
            or query in coin.get("symbol", "").casefold()
        ][:5]
        search_results.controls = [
            ft.FilledButton(
                content=ft.Text(f"+ {coin.get('name', '--')} ({coin.get('symbol', '--').upper()})", size=11),
                height=24,
                style=ft.ButtonStyle(
                    bgcolor=COLORS["surface_alt"],
                    color=COLORS["text"],
                ),
                on_click=lambda event, selected_coin=coin: add_preference(selected_coin),
            )
            for coin in matches
        ]
        page.update()

    def toggle_preference(display_name: str) -> None:
        if display_name in selected_preferences:
            selected_preferences.remove(display_name)
            preference_options.pop(display_name, None)
        else:
            selected_preferences.add(display_name)
        render_preference_buttons()
        render_rows()

    def render_preference_buttons() -> None:
        preference_buttons.controls = [
            ft.FilledButton(
                content=ft.Text(
                    f"{'-' if display_name in selected_preferences else '+'} {display_name}",
                    size=11,
                ),
                col={"xs": 6, "sm": 4, "md": 2},
                expand=True,
                height=24,
                tooltip=(
                    "Retirar da tabela"
                    if display_name in selected_preferences
                    else "Adicionar à tabela"
                ),
                style=ft.ButtonStyle(
                    bgcolor=(COLORS["teal"] if display_name in selected_preferences else COLORS["surface_alt"]),
                    color=(COLORS["background"] if display_name in selected_preferences else COLORS["text"]),
                ),
                on_click=lambda event, name=display_name: toggle_preference(name),
            )
            for display_name in preference_options
        ]

    def refresh_monitor_alerts() -> None:
        try:
            threshold = float((alert_threshold.value or "").strip().replace("%", "").replace(",", "."))
            if threshold <= 0:
                raise ValueError
        except ValueError:
            threshold = None
        try:
            positive_threshold = float(
                (positive_alert_threshold.value or "").strip().replace("%", "").replace(",", ".")
            )
            if positive_threshold <= 0:
                raise ValueError
        except ValueError:
            positive_threshold = None

        negative_alerts = []
        positive_alerts = []
        for coin in market_data:
            display_name = coin.get("name", "--")
            symbol = coin.get("symbol", "--").upper()
            quote = quote_for(coin) or {}
            samples = price_history.get(market_history_key(coin), [])

            def change_for_interval(interval: str) -> float | None:
                if interval == "1h":
                    return quote.get("percentChange1h")
                if interval == "24h":
                    return quote.get("percentChange24h")
                return history_change(samples, interval)

            if threshold is not None:
                for interval in alert_intervals:
                    if interval not in negative_alert_intervals:
                        continue
                    change = change_for_interval(interval)
                    if change is not None and change <= -threshold:
                        negative_alerts.append(
                            f"{display_name} ({symbol}) {interval}: {format_change(change)}"
                        )

            if positive_threshold is not None:
                for interval in alert_intervals:
                    if interval not in positive_alert_intervals:
                        continue
                    change = change_for_interval(interval)
                    if change is not None and change >= positive_threshold:
                        positive_alerts.append(
                            f"{display_name} ({symbol}) {interval}: {format_change(change)}"
                        )

        monitor_alert.visible = bool(negative_alerts)
        monitor_alert.bgcolor = "#351B1A"
        monitor_alert.border = ft.Border.all(1, COLORS["red"])
        monitor_alert_text.color = COLORS["red"]
        monitor_alert_text.value = (
            f"ALERTA DE QUEDA (>= {threshold:g}%)\n" + "\n".join(negative_alerts)
            if negative_alerts and threshold is not None
            else ""
        )
        positive_monitor_alert.visible = bool(positive_alerts)
        positive_monitor_alert.bgcolor = "#153126"
        positive_monitor_alert.border = ft.Border.all(1, COLORS["green"])
        positive_alert_text.color = COLORS["green"]
        positive_alert_text.value = (
            f"ALERTA DE ALTA (>= {positive_threshold:g}%)\n" + "\n".join(positive_alerts)
            if positive_alerts and positive_threshold is not None
            else ""
        )
        alert_results.controls = []
        if negative_alerts:
            monitor_alert.col = {"xs": 12, "md": 6 if positive_alerts else 12}
            alert_results.controls.append(monitor_alert)
        if positive_alerts:
            positive_monitor_alert.col = {"xs": 12, "md": 6 if negative_alerts else 12}
            alert_results.controls.append(positive_monitor_alert)
        rows.columns = build_columns()
        render_rows()

    async def check_email_alerts() -> None:
        for rule in email_rules:
            coin = next(
                (
                    item
                    for item in market_data
                    if item.get("symbol", "").upper() == rule["coin"]
                    or item.get("name", "").casefold() == rule["coin"].casefold()
                ),
                None,
            )
            if coin is None:
                continue
            quote = quote_for(coin) or {}
            interval = rule["interval"]
            if interval == "1h":
                change = quote.get("percentChange1h")
            elif interval == "24h":
                change = quote.get("percentChange24h")
            else:
                change = history_change(price_history.get(market_history_key(coin), []), interval)
            if change is None:
                continue

            is_falling = rule["direction"] == "Queda"
            triggered = change <= -rule["threshold"] if is_falling else change >= rule["threshold"]
            if not triggered:
                rule["armed"] = True
                continue
            if not rule["armed"]:
                continue

            symbol = coin.get("symbol", "--").upper()
            name = coin.get("name", symbol)
            direction_label = "queda" if is_falling else "alta"
            subject = f"Alerta de {direction_label}: {name} ({symbol})"
            body = (
                f"{name} ({symbol}) atingiu uma {direction_label} de {format_change(change)} "
                f"no período de {interval}.\n\n"
                f"Limite configurado: {rule['threshold']:g}%\n"
                f"Preço atual: {format_price(quote.get('price'))}\n"
                f"Verificado em: {datetime.now().astimezone().strftime('%d/%m/%Y %H:%M:%S')}"
            )
            try:
                await asyncio.to_thread(
                    send_alert_email,
                    rule["smtp_host"],
                    rule["smtp_port"],
                    rule["smtp_user"],
                    rule["smtp_password"],
                    rule["sender"],
                    rule["recipient"],
                    subject,
                    body,
                )
            except (OSError, smtplib.SMTPException, TimeoutError) as error:
                status.value = f"Falha ao enviar alerta por e-mail: {error}"
                page.update()
                continue
            rule["armed"] = False
            status.value = f"Alerta de {direction_label} enviado para {rule['recipient']} ({symbol})."
            page.update()

    def render_rows() -> None:
        visible = []
        for display_name, symbols in preference_options.items():
            if display_name not in selected_preferences:
                continue
            matching = [coin for coin in market_data if coin.get("symbol", "").upper() in symbols]
            if matching:
                selected = next(
                    (coin for coin in matching if coin.get("symbol", "").upper() == "POL"),
                    matching[0],
                )
                visible.append((display_name, selected))
        visible.sort(key=lambda item: item[0].casefold())
        visible = [coin for _, coin in visible]

        def price_chart(coin: dict) -> ft.Control:
            samples = price_history.get(market_history_key(coin), [])[-18:]
            values = [price for _, price in samples]
            if not values:
                return ft.Container(
                    content=ft.Text("--", size=11, color=COLORS["muted"]),
                    alignment=ft.Alignment(0, 0),
                    width=100,
                    height=40,
                    bgcolor="#12332F",
                    border=ft.Border.all(1, COLORS["teal"]),
                    border_radius=4,
                )
            low = min(values)
            high = max(values)
            span = high - low or 1
            rising = values[-1] >= values[0]
            color = COLORS["green"] if rising else COLORS["red"]
            bars = [
                ft.Container(
                    width=2,
                    height=8 + ((value - low) / span) * 28,
                    bgcolor=color,
                    border_radius=1,
                )
                for value in values
            ]
            return ft.Container(
                content=ft.Row(
                    controls=bars,
                    spacing=1,
                    height=34,
                    vertical_alignment=ft.CrossAxisAlignment.END,
                ),
                alignment=ft.Alignment(0, 0),
                width=82,
                height=40,
                padding=3,
                bgcolor="#12332F",
                border=ft.Border.all(1, COLORS["teal"]),
                border_radius=4,
            )

        def centered_cell(
            content: ft.Control,
            width: int,
            bgcolor: str | None = None,
        ) -> ft.DataCell:
            return ft.DataCell(
                ft.Container(
                    content=content,
                    alignment=ft.Alignment(0, 0),
                    width=width,
                    bgcolor=bgcolor,
                )
            )

        def period_cell(value: float | None, interval: str, width: int) -> ft.DataCell:
            background, _ = period_style(interval)
            return centered_cell(
                ft.Text(
                    format_change(value),
                    size=11 if width == 45 else 12,
                    color=COLORS["green"] if (value or 0) >= 0 else COLORS["red"],
                    weight=ft.FontWeight.BOLD if background else ft.FontWeight.NORMAL,
                ),
                width,
                background,
            )

        rows.rows = []
        for coin in visible:
            quote = quote_for(coin) or {}
            _, year = format_listing_date(coin)
            changes = [
                quote.get("percentChange1h"),
                quote.get("percentChange24h"),
                quote.get("percentChange7d"),
                quote.get("percentChange30d"),
                quote.get("percentChange1y"),
            ]
            short_history = price_history.get(market_history_key(coin), [])
            cells = [
                centered_cell(ft.Text(coin.get("name", "--"), size=12, weight=ft.FontWeight.W_600), 145),
                centered_cell(ft.Text(coin.get("symbol", "--").upper(), size=12), 58),
                centered_cell(ft.Text(year, size=12), 48),
                centered_cell(ft.Text(format_price(quote.get("price")), size=12), 88),
                centered_cell(price_chart(coin), 82),
            ]
            cells.extend(
                period_cell(history_change(short_history, interval), interval, 45)
                for interval in ("1m", "5m", "30m")
            )
            cells.extend(
                [
                    period_cell(changes[0], "1h", 52),
                    period_cell(changes[1], "24h", 52),
                    centered_cell(ft.Text(format_change(changes[2]), size=12, color=COLORS["green"] if (changes[2] or 0) >= 0 else COLORS["red"]), 52),
                    centered_cell(ft.Text(format_change(changes[3]), size=12, color=COLORS["green"] if (changes[3] or 0) >= 0 else COLORS["red"]), 52),
                    centered_cell(ft.Text(format_change(changes[4]), size=12, color=COLORS["green"] if (changes[4] or 0) >= 0 else COLORS["red"]), 58),
                ]
            )
            rows.rows.append(
                ft.DataRow(
                    cells=cells
                )
            )
        if page.controls:
            page.update()

    async def update_data() -> None:
        status.value = "Atualizando cotações..."
        page.update()
        try:
            market_data[:] = await asyncio.to_thread(fetch_market_data)
            sampled_at = datetime.now().timestamp()
            active_history_keys = set()
            for coin in market_data:
                quote = quote_for(coin) or {}
                price = quote.get("price")
                if isinstance(price, (int, float)):
                    history_key = market_history_key(coin)
                    samples = price_history.setdefault(history_key, [])
                    samples.append((sampled_at, float(price)))
                    del samples[:-32]
                    active_history_keys.add(history_key)
            for history_key in price_history.keys() - active_history_keys:
                del price_history[history_key]
            render_preference_buttons()
            last_updated.value = "Atualizado em " + datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S")
            status.value = f"Dados do CoinMarketCap | Busca nos {len(market_data):,} ativos carregados".replace(",", ".")
            refresh_monitor_alerts()
            await check_email_alerts()
            render_rows()
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            status.value = f"Falha ao consultar CoinMarketCap: {error}"
            page.update()

    async def refresh_periodically() -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            await update_data()

    def refresh_now(_: ft.ControlEvent) -> None:
        page.run_task(update_data)

    email_config_button = ft.IconButton(
        icon=ft.Icons.EMAIL_OUTLINED,
        icon_color=COLORS["orange"],
        tooltip="Configurar alertas por e-mail",
        on_click=open_email_dialog,
    )

    page.add(
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.ResponsiveRow(
                    columns=12,
                    spacing=4,
                    run_spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Container(
                            col={"xs": 12, "md": 3},
                            content=ft.Column(
                                spacing=2,
                                controls=[
                                    ft.Text(
                                        "MERCADO CRIPTO",
                                        size=24,
                                        weight=ft.FontWeight.BOLD,
                                        color=COLORS["teal"],
                                        font_family="Bahnschrift SemiBold",
                                        style=ft.TextStyle(letter_spacing=2),
                                    ),
                                    ft.Text("Cotações ao vivo do CoinMarketCap", size=12, color=COLORS["muted"]),
                                    search,
                                ],
                            ),
                        ),
                        ft.Container(
                            col={"xs": 12, "md": 1},
                            alignment=ft.Alignment(0, 0),
                            content=email_config_button,
                        ),
                        ft.Container(
                            col={"xs": 12, "md": 5},
                            margin=ft.Margin.only(top=14),
                            content=ft.ResponsiveRow(
                                columns=12,
                                spacing=4,
                                run_spacing=2,
                                alignment=ft.MainAxisAlignment.CENTER,
                                controls=[
                                    ft.Container(
                                        col={"xs": 12, "md": 6},
                                        alignment=ft.Alignment(0, 0),
                                        content=ft.Column(
                                            spacing=-4,
                                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=[alert_threshold, negative_interval_choices],
                                        ),
                                    ),
                                    ft.Container(
                                        col={"xs": 12, "md": 6},
                                        alignment=ft.Alignment(0, 0),
                                        content=ft.Column(
                                            spacing=-4,
                                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=[positive_alert_threshold, positive_interval_choices],
                                        ),
                                    ),
                                ],
                            ),
                        ),
                        ft.Container(
                            col={"xs": 12, "md": 3},
                            alignment=ft.Alignment(1, 0),
                            content=ft.Column(
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                                spacing=0,
                                controls=[
                                    ft.IconButton(
                                        icon=ft.Icons.REFRESH,
                                        icon_color=COLORS["teal"],
                                        tooltip="Atualizar agora",
                                        on_click=refresh_now,
                                    ),
                                    last_updated,
                                ],
                            ),
                        ),
                    ],
                ),
                ft.ResponsiveRow(
                    expand=True,
                    columns=12,
                    spacing=8,
                    run_spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Column(
                            col={"xs": 12, "md": 9},
                            spacing=8,
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Column(
                                    spacing=2,
                                    controls=[
                                        search_results,
                                        ft.Container(height=2),
                                        preference_buttons,
                                    ],
                                ),
                                table_frame,
                                ft.Container(height=4),
                                alert_results,
                                status,
                            ],
                        ),
                        ft.Container(
                            col={"xs": 12, "md": 3},
                            padding=11,
                            bgcolor=COLORS["surface"],
                            border=ft.Border.all(1, COLORS["border"]),
                            border_radius=6,
                            content=ft.Column(
                                expand=True,
                                spacing=10,
                                controls=[
                                    ft.Row(
                                        spacing=8,
                                        controls=[
                                            ft.Container(
                                                width=52,
                                                height=52,
                                                border_radius=26,
                                                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                                content=ft.Image(
                                                    src="botcrypto.png",
                                                    width=52,
                                                    height=52,
                                                    fit=ft.BoxFit.COVER,
                                                    semantics_label="Avatar gráfico do CriptoBot",
                                                ),
                                            ),
                                            ft.Column(
                                                spacing=2,
                                                controls=[
                                                    ft.Text("CriptoBot", size=13, weight=ft.FontWeight.BOLD, color=COLORS["text"]),
                                                    ft.Text("Assistente de mercado", size=9, color=COLORS["muted"]),
                                                ],
                                            ),
                                        ],
                                    ),
                                    ft.Divider(height=1, color=COLORS["border"]),
                                    conversation,
                                    ft.Row(
                                        spacing=6,
                                        controls=[
                                            question,
                                            ft.IconButton(
                                                icon=ft.Icons.CLEAR,
                                                icon_color=COLORS["muted"],
                                                tooltip="Limpar pesquisa e resultado",
                                                on_click=clear_question,
                                            ),
                                            ft.IconButton(
                                                icon=ft.Icons.SEND,
                                                icon_color=COLORS["orange"],
                                                tooltip="Enviar pergunta",
                                                on_click=send_question,
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ),
                    ],
                ),
            ],
        )
    )
    if not page.web:
        page.window.on_event = handle_window_event
        page.on_resize = handle_page_resize
    render_preference_buttons()
    page.run_task(update_data)
    page.run_task(refresh_periodically)


if __name__ == "__main__":
    ft.run(main, assets_dir=str(Path(__file__).parent / "assets"))