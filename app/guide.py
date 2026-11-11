"""The user guide of Lore Sentry, as the chat knows it.

`guide_data/{ko,en}.json` are the help topics the frontend ships
(`src/features/help/guide-topics.{ko,en}.ts`), exported with:

    node -e "import('./src/features/help/guide-topics.ko.ts').then(m => ...)"

Regenerate them when the help guide changes. The overview and the action catalog
below are written here because the guide has no page for them.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from html import escape
from pathlib import Path

DATA = Path(__file__).parent / "guide_data"
LOCALES = ("ko", "en")
HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")

ACTION_TAG = re.compile(r'<action\b[^>]*\bid\s*=\s*"([^"]*)"[^>]*>(?:\s*</action>)?')
ACTION_MARKER = re.compile(r"\[\[action:([^\]\s]+)\]\]")
MAX_ACTIONS = 3

VIEW_KINDS = (
    "new",
    "search",
    "graph",
    "timeline",
    "memo",
    "setting-check",
    "trash",
    "settings",
    "help",
)

OVERVIEW = {
    "ko": """Lore Sentry(로어 센트리)는 웹소설 작가를 위한 집필 에디터와 AI 에이전트입니다. 작품 하나가 프로젝트 하나이고, 프로젝트 안에 원고(회차), 설정 문서(캐릭터·장소·조직·아이템·이벤트·세계관), 메모, 문서 사이의 관계를 한곳에 둡니다. 쓰는 내용은 자동으로 저장되고 원고 본문은 서버에 암호화되어 저장됩니다.
로그인은 Google 계정으로 하며, 아이디와 비밀번호로 가입한 계정도 있습니다. 가입 직후에는 처음 안내(온보딩)가 나오고, 예시 프로젝트 ‘별이 없는 세자’를 만들어 둘러볼 수 있습니다.
작업공간 왼쪽 사이드바에는 위에서부터 계정 메뉴와 프로젝트 전환, 검색, 관계도, 타임라인, 메모, AI의 설정 문서 작성 제안, 설정 오류 점검, 즐겨찾기, 파일(원고와 설정 문서 폴더), 가져오기, 휴지통, 설정, 도움말이 있습니다. 오른쪽 아래 둥근 버튼이 AI 챗입니다.
AI 기능은 세 가지입니다. AI 챗은 원고·설정 문서·관계·메모를 읽고 작품에 대한 질문에 근거 문서를 붙여 답하며, 이 가이드를 바탕으로 사용법 질문에도 답합니다. AI의 설정 문서 작성 제안(작가들이 ‘최신화’라고도 부릅니다)은 바뀐 원고를 읽고 설정 문서에 더할 내용과 관계를 제안합니다. 설정 오류 점검은 회차 원고가 설정 문서·앞 회차와 어긋난 곳을 찾습니다. AI는 어떤 경우에도 문서를 직접 고치지 않고, 제안과 판정은 작가가 확정합니다.
가져오기는 텍스트(.txt)와 마크다운(.md)만 받습니다. docx·hwp·xlsx 가져오기는 지원하지 않으며, 내보내기의 docx·hwp도 준비 중입니다. AI 챗은 하루 사용량 한도가 있고 매일 자정에 초기화됩니다. 의견은 도움말의 ‘피드백 보내기’로 보냅니다.""",
    "en": """Lore Sentry is a writing editor and AI agent for web novel authors. One story is one project; a project keeps its chapters, setting documents (characters, places, organizations, items, events, worldbuilding), notes and the relationships between documents in one place. Everything saves automatically and manuscript bodies are stored encrypted on the server.
You sign in with a Google account; some accounts sign in with an id and password. Right after signing up an onboarding guide runs, and you can create the sample project “유리 정원의 기록” to look around.
The sidebar on the left of the workspace holds, from top to bottom: the account menu and project switcher, Search, Relationship map, Timeline, Notes, Refresh setting docs, Setting check, Favorites, Files (chapter and setting document folders), Import, Trash, Settings and Help. The round button at the bottom right is AI chat.
There are three AI features. AI chat reads the chapters, setting documents, relationships and notes and answers questions about the story with source documents attached; it also answers questions about using the tool from this guide. Refresh setting docs reads changed chapters and proposes additions and relationships for the setting documents. Setting check finds where a chapter contradicts the setting documents or earlier chapters. The AI never edits documents itself; the author confirms every proposal and verdict.
Import accepts text (.txt) and Markdown (.md) only. Importing docx, hwp or xlsx is not supported, and docx and hwp export are still coming. AI chat has a daily usage limit that resets at midnight. Send opinions through “Send feedback” in Help.""",
}

ACTION_NOTES = {
    "ko": {
        "tour": "작업공간 둘러보기(화면 곳곳을 짚어 주는 안내)를 시작합니다. 처음 쓰는 작가에게 권합니다.",
        "import": "가져오기 창을 엽니다(txt·md 파일을 원고로).",
        "view:new": "새 탭을 엽니다. 원고·설정 문서를 새로 만들 수 있습니다.",
        "view:search": "프로젝트 검색 화면을 엽니다.",
        "view:graph": "관계도를 엽니다.",
        "view:timeline": "타임라인을 엽니다.",
        "view:memo": "메모 화면을 엽니다.",
        "view:setting-check": "설정 오류 점검 화면을 엽니다.",
        "view:trash": "휴지통을 엽니다.",
        "view:settings": "프로젝트 설정을 엽니다.",
        "view:help": "사용 가이드 목록을 엽니다.",
    },
    "en": {
        "tour": "Starts the workspace tour that points out each part of the screen. Recommended for a new author.",
        "import": "Opens the import dialog (txt and md files become chapters).",
        "view:new": "Opens a new tab where chapters and setting documents can be created.",
        "view:search": "Opens project search.",
        "view:graph": "Opens the relationship map.",
        "view:timeline": "Opens the timeline.",
        "view:memo": "Opens notes.",
        "view:setting-check": "Opens the setting check screen.",
        "view:trash": "Opens the trash.",
        "view:settings": "Opens project settings.",
        "view:help": "Opens the list of guide topics.",
    },
}

ROUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {"kind": {"type": "string", "enum": ["story", "product", "mixed"]}},
}


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    body: str


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    summary: str
    lead: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class Guide:
    locale: str
    topics: tuple[Topic, ...]

    def topic(self, topic_id: str) -> Topic | None:
        return next((topic for topic in self.topics if topic.id == topic_id), None)

    def has_section(self, topic_id: str, section_id: str) -> bool:
        topic = self.topic(topic_id)
        return topic is not None and any(s.id == section_id for s in topic.sections)


@cache
def load(locale: str) -> Guide:
    if locale not in LOCALES:
        locale = "ko"
    raw = json.loads((DATA / f"{locale}.json").read_text(encoding="utf-8"))
    topics = tuple(
        Topic(
            id=item["id"],
            title=item["title"],
            summary=item["summary"],
            lead=item["lead"],
            sections=tuple(
                Section(s["id"], s["title"], s["body"]) for s in item["sections"]
            ),
        )
        for item in raw
    )
    return Guide(locale, topics)


def guide_locale(question: str, locale: str | None) -> str:
    if HANGUL.search(question):
        return "ko"
    if locale in LOCALES:
        return locale
    return "ko"


def attr(value: str) -> str:
    return escape(value, quote=True)


def topics_xml(guide: Guide) -> str:
    return "\n".join(
        f'<topic id="{attr(t.id)}" title="{attr(t.title)}">{escape(t.summary)}</topic>'
        for t in guide.topics
    )


def actions_xml(guide: Guide) -> str:
    notes = ACTION_NOTES[guide.locale]
    lines = [
        f'<action id="{name}">{escape(note)}</action>' for name, note in notes.items()
    ]
    guide_note = (
        "사용 가이드의 그 절을 엽니다. topic과 section은 <guide>의 id입니다."
        if guide.locale == "ko"
        else "Opens that section of the guide; topic and section are ids from <guide>."
    )
    lines.append(
        f'<action id="guide:&lt;topic&gt;#&lt;section&gt;">{guide_note}</action>'
    )
    return "<actions>\n" + "\n".join(lines) + "\n</actions>"


def guide_xml(guide: Guide) -> str:
    parts = [
        f'<guide locale="{guide.locale}">',
        f"<overview>\n{OVERVIEW[guide.locale]}\n</overview>",
    ]
    for topic in guide.topics:
        parts.append(
            f'<topic id="{attr(topic.id)}" title="{attr(topic.title)}">\n'
            f"<lead>{escape(topic.lead)}</lead>"
        )
        for section in topic.sections:
            parts.append(
                f'<section id="{attr(section.id)}" title="{attr(section.title)}">\n'
                f"{escape(section.body)}\n</section>"
            )
        parts.append("</topic>")
    parts.append("</guide>")
    parts.append(actions_xml(guide))
    return "\n".join(parts)


def valid_action(action: str, guide: Guide) -> bool:
    if action in ("tour", "import"):
        return True
    if action.startswith("view:"):
        return action[len("view:") :] in VIEW_KINDS
    if action.startswith("guide:"):
        target = action[len("guide:") :]
        topic_id, _, section_id = target.partition("#")
        if section_id:
            return guide.has_section(topic_id, section_id)
        return guide.topic(topic_id) is not None
    return False


def parse_actions(text: str, guide: Guide) -> tuple[str, list[str]]:
    """Take the <action id="…"/> tags out of an answer and keep the valid ids."""
    actions: list[str] = []
    for match in ACTION_TAG.finditer(text):
        action = match.group(1).strip()
        if action in actions or not valid_action(action, guide):
            continue
        actions.append(action)
    clean = ACTION_TAG.sub("", text)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, actions[:MAX_ACTIONS]


def with_markers(text: str, actions: list[str]) -> str:
    if not actions:
        return text
    markers = "".join(f"[[action:{action}]]" for action in actions)
    return f"{text}\n{markers}" if text else markers


def strip_markers(text: str) -> str:
    return ACTION_MARKER.sub("", text).rstrip()
