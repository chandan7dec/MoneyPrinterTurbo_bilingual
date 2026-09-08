import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path

from loguru import logger

from app.config import config
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import llm
from app.services import state as sm
from app.services import task as tm
from app.services.loomloom import LoomLoomConfirmedVideoRequest
from app.utils.logging_utils import format_log_record


# WebUI 的配置保存在进程级全局字典中。普通任务仍然固定并发数为 1，延续
# 原有配置一致性；中英双语对会占用一个队列槽位，并在槽位内部以两线程并发
# 渲染英文与印地语两条流水线（见 _run_bilingual_pair），印地语视频不再等待
# 英文视频先完成。
_task_manager = InMemoryTaskManager(
    max_concurrent_tasks=1,
    max_queued_tasks=max(1, int(config.app.get("max_queued_tasks", 100))),
)
_task_logs: dict[str, deque[str]] = {}
_task_logs_lock = threading.RLock()
_MAX_LOG_TASKS = 20
_MAX_LOG_RECORDS_PER_TASK = 1000
# Streamlit 无法由后台线程直接推送组件更新，只能通过 Fragment 轮询。0.5 秒
# 足以让 WebUI 日志接近终端实时输出，又不会像高频刷新那样持续占用浏览器资源。
TASK_LOG_REFRESH_INTERVAL_SECONDS = 0.5


def _append_task_log(task_id: str, message: str) -> None:
    """按任务保存有限数量的日志，供 Streamlit Fragment 安全轮询。"""
    with _task_logs_lock:
        records = _task_logs.get(task_id)
        if records is None:
            # 只保留最近任务的日志，避免 WebUI 服务长时间运行后持续占用内存。
            # dict 保持插入顺序；任务日志仅用于界面诊断，淘汰最早记录不影响任务。
            if len(_task_logs) >= _MAX_LOG_TASKS:
                oldest_task_id = next(iter(_task_logs))
                _task_logs.pop(oldest_task_id, None)
            records = deque(maxlen=_MAX_LOG_RECORDS_PER_TASK)
            _task_logs[task_id] = records
        records.append(message.rstrip())


def get_task_logs(task_id: str) -> list[str]:
    """返回日志快照，避免页面渲染期间持有后台线程使用的锁。"""
    with _task_logs_lock:
        return list(_task_logs.get(task_id, ()))


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INTRO_OUTRO_SCRIPT = _PROJECT_ROOT / "scripts" / "add_intro_outro.py"


def _is_playable_mp4(video_path: Path) -> bool:
    """True when the mp4 is complete: readable moov atom (index) + decodable.

    A file that is still being written, or was killed mid-encode, has no moov
    atom yet and every player reports "not playable" — this catches it before
    the task is marked done.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def _intro_outro_enabled() -> bool:
    return os.environ.get("MPT_INTRO_OUTRO", "1").strip().lower() not in {"0", "false", "no"}


def _strip_gender_suffix(voice_name: str) -> str:
    """Map WebUI names like en-US-JennyNeural-Female back to the Edge TTS name."""
    name = voice_name.strip()
    name = name.replace("-Female", "").replace("-Male", "").replace("-V2", "").strip()
    return name


def _add_intro_outro_cards(
    task_id: str,
    params: VideoParams,
    result: dict,
) -> dict:
    """Add narrated intro + outro cards to every final video (WebUI flow).

    The WebUI history scanner and task state both point at ``final-<n>.mp4``,
    so the enhanced render is written to a temporary file first and then
    atomically replaces the original final file. A failure never fails the
    task: the plain video stays in place and the error is logged.
    """
    if not _intro_outro_enabled():
        logger.info(f"intro/outro cards disabled via MPT_INTRO_OUTRO=0, task_id={task_id}")
        return result
    videos = result.get("videos") or []
    if not videos:
        return result
    if not _INTRO_OUTRO_SCRIPT.is_file():
        logger.warning(f"intro/outro script not found, skipping: {_INTRO_OUTRO_SCRIPT}")
        return result
    # Run the card script with the SAME interpreter that runs this process.
    # Requiring the external "uv" binary made the step silently skip on
    # machines without uv (Google Colab, plain venv installs) — the video
    # rendered fine but never got its intro/outro cards.
    python_cmd = sys.executable or shutil.which("python") or shutil.which("python3")
    if not python_cmd:
        logger.warning("no python interpreter found, skipping intro/outro cards")
        return result

    subject = str(params.video_subject or "").strip()
    title = subject or str(params.video_script or "").strip()[:60] or task_id
    voice = _strip_gender_suffix(str(params.voice_name or "")) or "en-US-JennyNeural"

    sm.state.patch_task(task_id, state=const.TASK_STATE_PROCESSING, progress=99)
    try:
        for video in videos:
            video_path = Path(video)
            if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
                continue
            temp_out = video_path.with_name(f".{video_path.stem}-cards{video_path.suffix}")
            logger.info(f"adding intro/outro cards: {video_path.name}")
            proc = subprocess.run(
                [
                    str(python_cmd), str(_INTRO_OUTRO_SCRIPT),
                    "--video", str(video_path),
                    "--title", title,
                    "--voice", voice,
                    # 每支成片都会重新生成一支动态的柔和丝感渐变卡片，
                    # 而不是从画面里取样出近乎相同的浅色，保证不同视频
                    # 之间的卡片颜色彼此不同且更吸引眼球。
                    "--palette", "silk",
                    "--out", str(temp_out),
                ],
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                errors="replace",
            )
            if proc.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 0:
                temp_out.replace(video_path)  # atomic in-place replace
                logger.info(f"intro/outro cards added: {video_path.name}")
            else:
                tail = (proc.stdout or "").splitlines()[-10:]
                tail += (proc.stderr or "").splitlines()[-5:]
                logger.warning(
                    f"intro/outro failed for {video_path.name}, keeping original: "
                    + " | ".join(tail[-3:])
                )
                if temp_out.exists():
                    try:
                        temp_out.unlink()
                    except OSError:
                        pass
    except Exception as exc:
        logger.exception(
            f"unexpected intro/outro failure, task_id={task_id}, error={exc}"
        )
    finally:
        sm.state.patch_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100)
    return result


def _run_generation_worker(
    task_id: str,
    params: VideoParams,
    capture_logs: bool,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
) -> dict:
    """
    在后台线程中执行现有视频流水线。配置锁由调用方统一持有：普通单任务由
    ``_run_generation`` 持锁；中英双语对由 ``_run_bilingual_pair`` 为整对任务
    持一把锁，两个线程在同一份稳定配置下并发渲染，不会互相等待。

    Loguru 的 sink 是进程级资源，因此必须按当前工作线程过滤。否则同时运行的
    API 任务或其它页面日志会混入当前任务。页面只读取普通列表快照，不会从后台
    线程访问 Streamlit session_state，从根源上避免刷新时的 delta 路径错乱。
    """
    log_handler_id = None
    worker_thread_id = threading.get_ident()
    try:
        if capture_logs:
            log_handler_id = logger.add(
                lambda message: _append_task_log(task_id, str(message)),
                level="DEBUG",
                format=format_log_record,
                colorize=False,
                filter=lambda record: record["thread"].id == worker_thread_id,
            )

        result = tm.start(
            task_id=task_id,
            params=params,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
        )
        if result.get("videos"):
            for video in result["videos"]:
                video_path = Path(video)
                if video_path.is_file() and not _is_playable_mp4(video_path):
                    raise RuntimeError(
                        f"encoded video is incomplete and not playable "
                        f"(moov atom missing — render was interrupted): {video_path}"
                    )
            result = _add_intro_outro_cards(task_id, params, result)
        return result
    except Exception as exc:
        # tm.start 已负责把流水线异常转换成失败状态；这里额外保护日志 sink、
        # 配置锁等 WebUI 包装层。任何后台线程异常都必须留下终态，不能让任务
        # 管理器在工作线程退出后仍永久显示“生成中”。
        error = f"{type(exc).__name__}: {exc}"
        failure = {
            "task_id": task_id,
            "state": const.TASK_STATE_FAILED,
            "progress": 0,
            "failed_stage": "webui_worker",
            "error": error,
        }
        sm.state.update_task(
            task_id,
            state=failure["state"],
            progress=failure["progress"],
            failed_stage=failure["failed_stage"],
            error=failure["error"],
        )
        logger.exception(
            f"unexpected WebUI generation worker failure, "
            f"task_id={task_id}, error={exc}"
        )
        return failure
    finally:
        if log_handler_id is not None:
            try:
                logger.remove(log_handler_id)
            except ValueError:
                logger.debug(
                    f"WebUI task log handler already removed: task_id={task_id}"
                )


def _run_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
) -> dict:
    """
    单任务路径：在后台线程执行流水线前获取运行期配置锁。

    锁保证同一任务不会在生成中途切换 Provider、密钥等进程级全局配置。
    双语对任务不走这里（改由 ``_run_bilingual_pair`` 为整对任务持锁后并发
    调用 ``_run_generation_worker``），避免第二个任务排队等待第一个完成。
    """
    with config.runtime_config_lock():
        return _run_generation_worker(
            task_id=task_id,
            params=params,
            capture_logs=capture_logs,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
        )


_HINDI_VOICE = "hi-IN-MadhurNeural"
_HINDI_FONT = "NotoSansDevanagari-Bold.ttf"

_LATIN_OR_DIGIT_RE = re.compile(r"[A-Za-z0-9]")

_HINDI_SCRIPT_LINES = [
    "You are a native Hindi scriptwriter for short-form YouTube/Instagram videos (Shorts / Reels). Write narration in the Devanagari script the way a real Hindi-speaking Indian creator would speak to their audience — conversational, clear, and lively.",
    "",
    "## Target register",
    "Write the Hindi you actually hear in Indian short videos, news, and everyday speech — not textbook or overly Sanskritised Hindi. Use the words and phrasing a normal Hindi speaker in India would use when talking to friends or explaining something on camera.",
    "- Good (natural, spoken): \"भारत की आबादी लगभग १४० करोड़ है\"",
    "- Avoid (stiff / bookish): \"भारतीय जनसंख्या अंदाज़े से चौदह 아라ब व्यक्ति संख्या में है\"",
    "- Good (natural): \"लोग छोटी-छोटी किसान खेतों से रोज़ाना खाना मिलता है\"",
    "- Avoid (awkward word-for-word): \"लोग समय-समय पर लघु कृषि खेतों से प्रतिदिन भोजन प्राप्त करते हैं\"",
    "",
    "## Hard rules",
    "1. EVERY character must be Devanagari Hindi. No English words, no Latin letters, no Roman/Hinglish transliteration, no quoted English terms anywhere in the output.",
    "2. English-origin words that Indians genuinely say in Hindi are fine AND preferred when written in Devanagari: इंटरनेट, वीडियो, मोबाइल, फोन, ऐप, कंप्यूटर, न्यूज़, एआई, यूट्यूब, इंस्टाग्राम, व्हाट्सएप, ट्रेन, हवाई जहाज़, मार्केट, स्चूल, कॉलेज, हॉस्पिटल. Never replace these with awkward literal translations (e.g. do NOT say 'दूर-संचार' for 'इंटरनेट').",
    "3. Foreign names (Amazon, YouTube, Covid, iPhone, etc.) must be written phonetically in Devanagari as Indians say them: अमेज़न, यूट्यूब, कोविड, आईफ़ोन. Small numbers should be Hindi words (दो, पाँच, दस); larger numbers may use Devanagari digits ०-९.",
    "4. Use common everyday Hindi verbs and nouns. Prefer 'खाना' over 'भोजन', 'लोग' over 'जनसमूह', 'काम' over 'कार्य', 'बोला' over 'उदघोषित किया', 'देखा' over 'दृष्टिगत किया', 'पैसा' over 'मुद्रा', 'जगह' over 'स्थान', 'रास्ता' over 'मार्ग', 'घर' over 'निवास स्थान'.",
    "5. Keep short, simple sentences with correct gender and verb agreement. Natural word order (subject-object-verb). Avoid long, winding sentences.",
    "",
    "## Translation rules (when an English script is given below)",
    "- Translate the MEANING and TONE naturally into everyday Hindi. The result should read like an original Hindi script, NOT like a translation.",
    "- Keep the same story, same facts, same order — never drop facts, never add new ones, never change the message.",
    "- Never translate word-for-word. If an English phrase has no natural Hindi equivalent, rephrase it the way an Indian would actually say the same thing.",
    "- If the English script mentions a place, person, or thing Indians know by an English name, keep it in Devanagari phonetics (e.g. 'इंटरनेट' for 'internet', 'गूगल' for 'Google').",
    "",
    "## Output format",
    "- One flowing paragraph of about 150-165 words, engaging and easy to listen to.",
    "- Include an Indian example or reference where it fits naturally (e.g. a familiar city, food, festival, market, or daily-life scene).",
    "- End with one crisp, memorable takeaway sentence the viewer will remember.",
    "- Return ONLY the narration text in Devanagari. No quotes, no labels, no explanations, no English, no Latin letters.",
]
_HINDI_SCRIPT_SYSTEM_PROMPT = chr(10).join(_HINDI_SCRIPT_LINES)

_HINDI_TITLE_SYSTEM = (
    "You translate English video titles into short, catchy, natural Hindi titles written ONLY in the Devanagari script. "
    "Latin letters and Arabic digits are FORBIDDEN. "
    "If the title contains a number or acronym such as 5G, 3D, AI, write it the way people actually say it in Hindi "
    "(e.g. फाइव जी, थ्री डी, एआई). "
    "Use everyday Hindi words with correct grammar, like a real Indian YouTuber would title their Shorts/Reels — "
    "never a word-for-word translation. "
    "Prefer common catchy Hindi words like 'कैसे', 'क्या', 'राज़', 'कहानी', 'देखिए', 'जानिए', 'हैरान', 'गुप्त', 'आश्चर्य' over stiff alternatives. "
    "Output ONLY the Hindi title text, max 8 words, no quotes, no explanation."
)


def _generate_hindi_script(subject, source_script=""):
    """Generate a pure-Devanagari Hindi narration (retries until Latin-free)."""
    instruction = ""
    if str(source_script or "").strip():
        instruction = (
            chr(10) + chr(10)
            + "Below is an English narration. Rewrite it in everyday Hindi (Devanagari only) so it reads like an ORIGINAL Hindi script — same story, same facts, same order, but natural spoken Hindi, not word-for-word translation. Only output the Hindi narration, nothing else:"
            + chr(10) + chr(10)
            + source_script
        )
    script = ""
    for _ in range(3):
        try:
            script = llm.generate_script(
                video_subject=subject,
                language="Hindi",
                paragraph_number=1,
                custom_system_prompt=_HINDI_SCRIPT_SYSTEM_PROMPT + instruction,
            )
        except Exception as exc:
            logger.warning("Hindi script generation failed: " + str(exc)[:200])
            script = ""
        script = (script or "").strip()
        if script and not _LATIN_OR_DIGIT_RE.search(script):
            return script
    logger.warning("Hindi script still contains Latin text after retries; using best effort")
    return script


def _generate_hindi_title(subject):
    """Generate a short pure-Devanagari title for the video card."""
    try:
        title = llm.generate_script(
            video_subject=subject,
            language="Hindi",
            paragraph_number=1,
            custom_system_prompt=(
                _HINDI_TITLE_SYSTEM
                + chr(10) + chr(10)
                + "Translate this English video title into a catchy Hindi title: "
                + subject
            )
        )
    except Exception as exc:
        logger.warning("Hindi title generation failed: " + str(exc)[:200])
        return ""
    title = (title or "").strip()
    if _LATIN_OR_DIGIT_RE.search(title):
        return ""
    return title.splitlines()[0].strip() if title else ""


def build_hindi_pair_params(params):
    """Return a deep copy of params configured for a pure-Hindi twin video."""
    subject = str(params.video_subject or "").strip()
    source_script = str(params.video_script or "").strip()
    if not subject and not source_script:
        return None
    script = _generate_hindi_script(subject, source_script)
    if not script:
        return None
    hindi = params.model_copy(deep=True)
    hindi.video_script = script
    hindi.video_subject = _generate_hindi_title(subject) or subject
    hindi.video_language = "Hindi"
    hindi.voice_name = _HINDI_VOICE
    hindi.font_name = _HINDI_FONT
    hindi.custom_audio_file = None
    return hindi


def _mark_pair_task_failed(task_id: str, stage: str, error: str) -> None:
    """把无法继续的中英双语对任务写成可查询的失败终态。"""
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_FAILED,
        progress=0,
        failed_stage=stage,
        error=error,
    )


def _run_bilingual_pair(
    task_id: str,
    params: VideoParams,
    hindi_task_id: str,
    capture_logs: bool,
    voice_preview: dict | None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None,
) -> None:
    """为整对任务持一把运行期配置锁，并发执行主语言与印地语两条流水线。

    普通 WebUI 任务串行执行是为了让 Provider、密钥等进程级配置在生成期间
    保持稳定。这里让成对的英文与印地语视频在同一把锁内并发渲染：锁语义不变
    （生成期间其它会话仍不能改写配置），但第二个视频不再排队等待第一个完成，
    印地语成片与英文成片几乎同时交付。

    印地语脚本/标题的 LLM 翻译在印地语工作线程内完成，与英文视频渲染重叠，
    不会让提交页面等待翻译。
    """

    def run_primary():
        _run_generation_worker(
            task_id=task_id,
            params=params,
            capture_logs=capture_logs,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
        )

    def run_hindi():
        try:
            hindi_params = build_hindi_pair_params(params)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _mark_pair_task_failed(hindi_task_id, "hindi_pair", error)
            logger.exception(
                f"failed to build Hindi twin params, task_id={hindi_task_id}, "
                f"error={exc}"
            )
            return
        if hindi_params is None:
            _mark_pair_task_failed(
                hindi_task_id,
                "hindi_pair",
                "could not build a Hindi twin: script generation returned no "
                "Devanagari narration",
            )
            logger.warning(
                f"bilingual pair could not build a Hindi twin, task_id={task_id}"
            )
            return
        sm.state.update_task(
            hindi_task_id,
            video_subject=(
                hindi_params.video_subject
                or hindi_params.video_script
                or hindi_task_id
            ),
        )
        _run_generation_worker(
            task_id=hindi_task_id,
            params=hindi_params,
            capture_logs=capture_logs,
        )

    with config.runtime_config_lock():
        workers = [
            threading.Thread(target=run_primary, name=f"mpt-primary-{task_id[:8]}"),
            threading.Thread(target=run_hindi, name=f"mpt-hindi-{hindi_task_id[:8]}"),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()


def submit_bilingual_generation(
    task_id,
    params,
    capture_logs=True,
    voice_preview=None,
    loomloom_video_request=None,
):
    """Submit the primary-language task plus a pure-Hindi twin.

    The pair takes a single WebUI queue slot, but inside that slot the primary
    pipeline and the Hindi twin run concurrently under one runtime-config lock.
    The Hindi video therefore finishes at roughly the same time as the English
    one instead of waiting for it (both renders run at once, so the machine
    needs enough CPU/RAM for two videos). Returns the Hindi task id.
    """
    task_params = params.model_copy(deep=True)
    # 预览载荷与已确认请求都是不可变数据对象，只由后台线程读取；复制外层
    # 字典避免页面后续 rerun 替换缓存字段时影响已经入队的任务。
    voice_preview_snapshot = dict(voice_preview) if voice_preview else None
    loomloom_request_snapshot = loomloom_video_request
    hindi_task_id = str(uuid.uuid4())
    subject = task_params.video_subject or task_params.video_script or task_id

    # 两个任务的状态必须在线程启动前写入，页面本次脚本执行结束时即可查询。
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        video_subject=subject,
    )
    sm.state.update_task(
        hindi_task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        video_subject=subject,
    )
    try:
        _task_manager.add_task(
            _run_bilingual_pair,
            task_id=task_id,
            params=task_params,
            hindi_task_id=hindi_task_id,
            capture_logs=capture_logs,
            voice_preview=voice_preview_snapshot,
            loomloom_video_request=loomloom_request_snapshot,
        )
    except Exception as exc:
        # 调度失败必须让两个任务都成为可查询的失败终态，避免任务管理器永久
        # 显示“生成中”。保留异常类型便于从 Docker 或本机日志快速定位队列问题。
        error = f"{type(exc).__name__}: {exc}"
        for failed_id in (task_id, hindi_task_id):
            sm.state.update_task(
                failed_id,
                state=const.TASK_STATE_FAILED,
                progress=0,
                failed_stage="scheduling",
                error=error,
            )
        logger.exception(
            f"failed to submit bilingual generation, task_id={task_id}, error={exc}"
        )
        raise
    logger.info(
        "bilingual pair submitted: primary="
        + str(task_id)
        + ", hindi="
        + str(hindi_task_id)
    )
    return hindi_task_id


def submit_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool = True,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
) -> None:
    """
    登记并提交 WebUI 视频生成任务，调用后立即返回。

    任务状态必须在线程启动前写入。这样页面本次脚本执行结束时即可查询到任务，
    浏览器刷新或 WebSocket 重连也不依赖旧页面内存中的占位符。
    """
    task_params = params.model_copy(deep=True)
    # 预览载荷只包含不可变音频路径、参数快照和只读字幕时间轴。复制外层字典，
    # 避免页面后续 rerun 替换缓存字段时影响已经提交到后台队列的任务。
    voice_preview_snapshot = dict(voice_preview) if voice_preview else None
    # 已确认请求是冻结的数据对象，只在当前进程内传递。API Key 不会进入
    # VideoParams、任务状态、日志或落盘历史，也不会受后续页面 rerun 影响。
    loomloom_request_snapshot = loomloom_video_request
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        video_subject=task_params.video_subject or task_params.video_script or task_id,
    )
    try:
        _task_manager.add_task(
            _run_generation,
            task_id=task_id,
            params=task_params,
            capture_logs=capture_logs,
            voice_preview=voice_preview_snapshot,
            loomloom_video_request=loomloom_request_snapshot,
        )
    except Exception as exc:
        # 调度失败与流水线失败一样必须成为可查询状态，避免任务管理器永久显示
        # “生成中”。保留异常类型便于从 Docker 或本机日志快速定位队列问题。
        error = f"{type(exc).__name__}: {exc}"
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            failed_stage="scheduling",
            error=error,
        )
        logger.exception(
            f"failed to submit WebUI generation task, task_id={task_id}, error={exc}"
        )
        raise
