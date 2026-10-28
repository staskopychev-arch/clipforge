"""Bounded download of public YouTube video, without account cookies."""
import json,os,signal,subprocess,sys,tempfile
from pathlib import Path
from ..config import settings
from .media import probe_duration,probe_video_stream
from .y2save import Y2SaveDownloader,Y2SaveError,DownloadBlocked


class DirectDownloader:
    progress_callback=None

    def download(self,url,destination):
        Y2SaveDownloader._validate_youtube_url(url)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.direct-',dir=destination.parent) as directory:
            if self.progress_callback:self.progress_callback('downloading',None)
            options={'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,'stderr':subprocess.PIPE,'text':True}
            if os.name=='posix':options['start_new_session']=True
            process=subprocess.Popen([sys.executable,'-m','app.services.direct_download','--fetch',url,directory],**options)
            try:
                stdout,stderr=process.communicate(timeout=settings.download_provider_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                if os.name=='posix':os.killpg(process.pid,signal.SIGKILL)
                else:process.kill()
                process.communicate()
                raise Y2SaveError('Прямой загрузчик не выдал файл за отведённое время') from exc
            try:result=json.loads(stdout)
            except (ValueError,TypeError):raise Y2SaveError('Прямой загрузчик завершился без результата') from None
            if result.get('status')=='access_required':
                raise DownloadBlocked('YouTube требует входа или ограничил доступ. Автоматическое скачивание остановлено.')
            if process.returncode or result.get('status')!='ready':
                raise Y2SaveError('Прямой загрузчик не смог получить публичное видео')
            path=Path(directory)/'video.mp4'
            if not path.is_file() or path.is_symlink() or path.stat().st_size>settings.max_upload_bytes:
                raise Y2SaveError('Прямой загрузчик выдал пустой или слишком большой файл')
            probe_duration(path);probe_video_stream(path)
            path.replace(destination)
            return destination

    def close(self):pass


def fetch(url,directory):
    """Separate process provides a hard deadline for extraction and merging."""
    Y2SaveDownloader._validate_youtube_url(url)
    import playwright
    from yt_dlp import YoutubeDL
    class Log:
        def debug(self,message):pass
        def warning(self,message):pass
        def error(self,message):pass
    node=Path(playwright.__file__).parent/'driver'/('node.exe' if os.name=='nt' else 'node')
    if not node.is_file():return {'status':'runtime_unavailable'}
    options={'logger':Log(),'quiet':True,'noprogress':True,'cachedir':False,'noplaylist':True,
        'socket_timeout':15,'retries':0,'extractor_retries':0,'js_runtimes':{'node':{'path':str(node)}},
        'remote_components':[],'format':'bv*[height<=720]+ba/b[height<=720]/b','merge_output_format':'mp4',
        'outtmpl':str(Path(directory)/'input.%(ext)s'),'max_filesize':settings.max_upload_bytes}
    try:
        with YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False,ie_key='Youtube')
            if info.get('age_limit',0)>0 or info.get('availability') in {'private','premium_only','subscriber_only','needs_auth'}:
                return {'status':'access_required'}
            if info.get('is_live') or info.get('live_status') in {'is_live','is_upcoming'}:
                return {'status':'live_video'}
            ydl.process_info(info)
        files=[p for p in Path(directory).glob('input.*') if p.suffix in {'.mp4','.mkv','.webm'}]
        if len(files)!=1:return {'status':'file_unavailable'}
        files[0].replace(Path(directory)/'video.mp4')
        return {'status':'ready'}
    except Exception as exc:
        # Do not expose signed URLs, response bodies or account information.
        message=str(exc).casefold()
        restricted=('sign in','not a bot','age-restricted','members-only','private video',
            'not available in your country','not made this video available','http error 403','http error 429')
        return {'status':'access_required' if any(marker in message for marker in restricted) else 'unavailable'}


if __name__=='__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='--fetch':raise SystemExit(2)
    print(json.dumps(fetch(sys.argv[2],sys.argv[3])),flush=True)
