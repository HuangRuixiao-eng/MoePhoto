import os
import subprocess as sp
import re
import sys
import threading
import logging
import signal
from queue import Queue, Empty
from gevent import spawn_later, idle
from config import config
from imageProcess import clean, writeFile, BGR2RGB
from procedure import genProcess
from progress import Node, initialETA
from worker import context, begin

log = logging.getLogger('Moe')
ffmpegPath = os.path.realpath('ffmpeg/bin/ffmpeg') # require full path to spawn in shell
qOut = Queue(256)
stepVideo = [dict(op='buffer', bitDepth=16)]
pix_fmt = 'bgr48le'
pixBytes = 6
bufsize = 10 ** 8
isWindows = sys.platform[:3] == 'win'
reMatchInfo = re.compile(r'Stream #.*: Video:')
reSearchInfo = re.compile(r',[\s]*([\d]+)x([\d]+)[\s]*.+,[\s]*([.\d]+)[\s]*(fps|tbr)')
reMatchFrame = re.compile(r'frame=')
reSearchFrame = re.compile(r'frame=[\s]*([\d]+) ')
reMatchAudio = re.compile(r'Stream #0:1')
reMatchOutput = re.compile(r'Output #0,')
creationflag = sp.CREATE_NEW_PROCESS_GROUP if isWindows else 0
sigint = signal.CTRL_BREAK_EVENT if isWindows else signal.SIGINT
popen = lambda command: sp.Popen(command, stdout=sp.PIPE, stderr=sp.PIPE, bufsize=bufsize, creationflags=creationflag)
popenText = lambda command: sp.Popen(command, stderr=sp.PIPE, encoding='utf_8', errors='ignore')
insert1 = lambda t, s: ''.join((t[0], s, *t[1:]))
suffix = lambda p, s: insert1(os.path.splitext(p), s)
clipList = lambda l, start, end: l[:start] + l[end:]
commandVideoSkip = lambda command: clipList(command, 13, 23)

def removeFile(path):
  try:
    os.remove(path)
  except FileNotFoundError: pass
  except PermissionError as e:
    log.error(str(e))

def getVideoInfo(videoPath, by, width, height, frameRate):
  commandIn = [
    ffmpegPath,
    '-hide_banner',
    '-t', '1',
    '-f', 'lavfi',
    '-i', videoPath,
    '-map', '0:v:0',
    '-c', 'copy',
    '-f', 'null',
    '-'
  ]
  matchInfo = not (width and height and frameRate)
  matchFrame = not by
  matchOutput = True
  error = RuntimeError('Video info not found')
  videoOnly = True
  if by != 'cmd':
    commandIn = clipList(commandIn, 4, 6)
  if matchFrame:
    commandIn = clipList(commandIn, 2, 4)
  try:
    procIn = popenText(commandIn)
    totalFrames = 0

    while matchInfo or matchOutput or matchFrame:
      line = procIn.stderr.readline()
      if type(line) != str:
        line = str(line, 'utf-8', errors='ignore')
      sys.stdout.write(line)
      if not line:
        break
      line = line.lstrip()
      if reMatchOutput.match(line):
        matchOutput = False
      elif reMatchAudio.match(line):
        videoOnly = False
      if matchInfo and reMatchInfo.match(line):
        try:
          videoInfo = reSearchInfo.search(line).groups()
          if not width:
            width = int(videoInfo[0])
          if not height:
            height = int(videoInfo[1])
          if not frameRate:
            frameRate = float(videoInfo[2])
        except:
          log.error(line)
          raise error
        matchInfo = False
      if matchFrame and reMatchFrame.match(line):
        try:
          totalFrames = int(reSearchFrame.search(line).groups()[0])
        except:
          log.error(line)

    procIn.stderr.flush()
    procIn.stderr.close()
  finally:
    procIn.terminate()
  if matchInfo or (matchFrame and not totalFrames):
    raise error
  log.info('Info of video {}: {}x{}@{}fps, {} frames'.format(videoPath, width, height, frameRate, totalFrames))
  return width, height, frameRate, totalFrames, videoOnly

def enqueueOutput(out, queue):
  try:
    for line in iter(out.readline, b''):
      queue.put(line)
    out.flush()
  except: pass

def createEnqueueThread(pipe, *args):
  t = threading.Thread(target=enqueueOutput, args=(pipe, qOut, *args))
  t.daemon = True # thread dies with the program
  t.start()

def readSubprocess(q):
  while True:
    try:
      line = q.get_nowait()
      if not type(line) == str:
        line = str(line, encoding='utf_8', errors='replace')
    except Empty:
      break
    else:
      sys.stdout.write(line)

def prepare(video, by, steps):
  optEncode = steps[-1]
  encodec = optEncode.get('codec', config.defaultEncodec)  # pylint: disable=E1101
  optDecode = steps[0]
  decodec = optDecode.get('codec', config.defaultDecodec)  # pylint: disable=E1101
  optRange = steps[1]
  start = int(optRange.get('start', 0))
  outDir = config.outDir  # pylint: disable=E1101
  procSteps = stepVideo + list(steps[2:-1])
  diagnose = optEncode.get('diagnose', {})
  bench = diagnose.get('bench', False)
  clear = diagnose.get('clear', False)
  process, nodes = genProcess(procSteps)
  traceDetail = config.progressDetail or bench  # pylint: disable=E1101
  root = begin(Node({'op': 'video'}, 1, 2, 0), nodes, traceDetail, bench, clear)
  context.root = root
  slomos = [*filter((lambda opt: opt['op'] == 'slomo'), procSteps)]
  if start < 0:
    start = 0
  if start and len(slomos): # should generate intermediate frames between start-1 and start
    start -= 1
    for opt in slomos:
      opt['opt'].firstTime = 0
  stop = int(optRange.get('stop', -1))
  if stop <= start:
    stop = -1
  root.total = -1 if stop < 0 else stop - start
  outputPath = optEncode.get('file', '') or outDir + '/' + config.getPath()
  dataPath = suffix(outputPath, '-a')
  commandIn = [
    ffmpegPath,
    '-hide_banner',
    '-f', 'lavfi',
    '-i', video,
    '-vn',
    '-c', 'copy',
    '-y',
    dataPath,
    '-map', '0:v',
    '-f', 'rawvideo',
    '-pix_fmt', pix_fmt]
  if by != 'cmd':
    commandIn = clipList(commandIn, 2, 4)
  if len(decodec):
    commandIn.extend(decodec.split(' '))
  commandIn.append('-')
  metadata = ['-metadata', 'service_provider="MoePhoto {}"'.format(config.version)] # pylint: disable=E1101
  commandVideo = [
    ffmpegPath,
    '-hide_banner', '-y',
    '-f', 'rawvideo',
    '-pix_fmt', pix_fmt,
    '-s', '',
    '-r', '',
    '-i', '-',
    '-i', dataPath,
    '-map', '0:v',
    '-map', '1?',
    '-map', '-1:v',
    '-c:1', 'copy',
    *metadata,
    '-c:v:0'
  ] + encodec.split(' ') + ['']
  commandOut = None
  if by == 'file':
    commandVideo[14] = video
  else:
    commandVideo[-1] = suffix(outputPath, '-v')
    commandOut = [
      ffmpegPath,
      '-hide_banner', '-y',
      '-i', commandVideo[-1],
      '-i', dataPath,
      '-map', '0:v',
      '-map', '1?',
      '-c:0', 'copy',
      '-c:1', 'copy',
      *metadata,
      outputPath
    ]
  frameRate = optEncode.get('frameRate', 0)
  width = optDecode.get('width', 0)
  height = optDecode.get('height', 0)
  sizes = filter((lambda opt: opt['op'] == 'SR' or opt['op'] == 'resize'), procSteps)
  return outputPath, process, start, stop, root, commandIn, commandVideo, commandOut, slomos, sizes, width, height, frameRate

def setupInfo(by, outputPath, root, commandIn, commandVideo, commandOut, slomos, sizes, start, width, height, frameRate, totalFrames, videoOnly):
  if root.total < 0 and totalFrames > 0:
    root.total = totalFrames - start
  if frameRate:
    for opt in slomos:
      frameRate *= opt['sf']
  outWidth, outHeight = (width, height)
  for opt in sizes:
    if opt['op'] == 'SR':
      outWidth *= opt['scale']
      outHeight *= opt['scale']
    else: # resize
      outWidth = round(outWidth * opt['scaleW']) if 'scaleW' in opt else opt['width']
      outHeight = round(outHeight * opt['scaleH']) if 'scaleH' in opt else opt['height']
  commandVideo[8] = f'{outWidth}x{outHeight}'
  commandVideo[10] = str(frameRate)
  videoOnly |= start > 0
  if videoOnly or by != 'file':
    commandVideo = commandVideoSkip(commandVideo)
  if videoOnly or by == 'file':
    commandVideo[-1] = outputPath
    i = commandIn.index('-vn')
    commandIn = clipList(commandIn, i, i + 5)
    commandOut = None
  root.multipleLoad(width * height * 3)
  initialETA(root)
  root.reset().trace(0)
  return commandIn, commandVideo, commandOut

def cleanAV(command, path):
  if command:
    try:
      stat = os.stat(path)
    except Exception:
      stat = False
    removeFile(command[6])
    video = command[4]
    if stat:
      removeFile(video)
      return path
    else:
      return video

def mergeAV(command):
  if command:
    err = True
    procMerge = popenText(command)
    createEnqueueThread(procMerge.stderr)
    err, msg = procMerge.communicate()
    sys.stdout.write(msg)
    return procMerge, err
  else:
    return 0, 0

def SR_vid(video, by, *steps):
  def p(raw_image=None):
    bufs = process((raw_image, height, width))
    if (not bufs is None) and len(bufs):
      for buffer in bufs:
        if buffer:
          procOut.stdin.write(buffer)
    if raw_image:
      root.trace()

  outputPath, process, *args = prepare(video, by, steps)
  start, stop, root = args[:3]
  width, height, *more = getVideoInfo(video, by, *args[-3:])
  commandIn, commandVideo, commandOut = setupInfo(by, outputPath, *args[2:8], start, width, height, *more)
  procIn = popen(commandIn)
  procOut = sp.Popen(commandVideo, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE, bufsize=0)
  procMerge = 0
  err = 0

  try:
    createEnqueueThread(procOut.stdout)
    createEnqueueThread(procIn.stderr)
    createEnqueueThread(procOut.stderr)
    i = 0
    while (stop < 0 or i <= stop) and not context.stopFlag.is_set():
      raw_image = procIn.stdout.read(width * height * pixBytes) # read width*height*6 bytes (= 1 frame)
      if len(raw_image) == 0:
        break
      readSubprocess(qOut)
      if i >= start:
        p(raw_image)
      i += 1
      idle()
    os.kill(procIn.pid, sigint)
    p()

    procOut.communicate(timeout=300)
    procIn.terminate()
    readSubprocess(qOut)
    procMerge, err = mergeAV(commandOut)
  finally:
    log.info('Video processing end at frame #{}.'.format(i))
    procIn.terminate()
    procOut.terminate()
    if procMerge:
      procMerge.terminate()
    clean()
    try:
      if not by:
        removeFile(video)
    except:
      log.warning('Timed out waiting ffmpeg to terminate, need to remove {} manually.'.format(video))
    if err:
      log.warning('Unable to merge video and other tracks with exit code {}.'.format(err))
    else:
      outputPath = cleanAV(commandOut, outputPath)
  readSubprocess(qOut)
  return outputPath, i