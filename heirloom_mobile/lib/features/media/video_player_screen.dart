import 'dart:async';
import 'package:chewie/chewie.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:video_player/video_player.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

class VideoPlayerScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String id;
  final bool restart;

  const VideoPlayerScreen({
    super.key,
    required this.mediaType,
    required this.id,
    this.restart = false,
  });

  @override
  ConsumerState<VideoPlayerScreen> createState() => _VideoPlayerScreenState();
}

class _VideoPlayerScreenState extends ConsumerState<VideoPlayerScreen> {
  VideoPlayerController? _videoPlayerController;
  ChewieController? _chewieController;
  Timer? _progressTimer;

  bool _isLoading = true;
  String? _error;

  String _title = '';
  String? _subtitleLine;
  double _introStart = -1;
  double _introEnd = -1;
  double _creditsStart = -1;
  int? _nextEpisodeId;
  bool _opensubtitlesConfigured = false;
  bool _usedHls = false;

  String _streamUrl = '';
  String _hlsMasterUrl = '';
  String _selectedQuality = 'original'; // original (direct/master), 1080, 720, 480
  int _selectedAudioIndex = 0;
  int? _selectedSubTrackIndex; // index into _subtitleTracks or null for Off

  List<Map<String, dynamic>> _audioTracks = [];
  List<Map<String, dynamic>> _subtitleTracks = [];
  List<String> _availableResolutions = const ['original', '1080', '720', '480'];

  double _currentPosSeconds = 0;
  double _durationSeconds = 0;
  bool _introDismissed = false;
  bool _nextEpisodeDismissed = false;

  @override
  void initState() {
    super.initState();
    _loadPlaybackInfo();
  }

  Future<void> _loadPlaybackInfo() async {
    final session = ref.read(appSessionProvider);
    final serverUrl = session.serverUrl;
    final authToken = session.authToken;
    final dio = ref.read(apiClientProvider);

    if (serverUrl == null || authToken == null) {
      setState(() {
        _error = 'Not authenticated or server URL is missing.';
        _isLoading = false;
      });
      return;
    }

    try {
      final res = await dio.get('/api/playback/info/${widget.mediaType}/${widget.id}');
      final data = Map<String, dynamic>.from(res.data as Map);

      final title = data['title']?.toString() ?? 'Heirloom Video';
      final se = data['season_episode']?.toString() ?? '';
      final epTitle = data['episode_title']?.toString() ?? '';
      final subLine = [se, epTitle].where((s) => s.isNotEmpty).join(' • ');

      final caps = data['capabilities'] is Map ? Map<String, dynamic>.from(data['capabilities'] as Map) : const {};
      final prefs = data['preferences'] is Map ? Map<String, dynamic>.from(data['preferences'] as Map) : const {};

      _title = title;
      _subtitleLine = subLine.isNotEmpty ? subLine : null;
      _streamUrl = data['stream_url']?.toString() ?? '/stream/${widget.mediaType}/${widget.id}';
      _hlsMasterUrl = data['hls_master_url']?.toString() ?? '/hls/${widget.mediaType}/${widget.id}/master.m3u8';
      _durationSeconds = (data['duration'] as num?)?.toDouble() ?? 0.0;
      _introStart = (data['intro_start'] as num?)?.toDouble() ?? -1;
      _introEnd = (data['intro_end'] as num?)?.toDouble() ?? -1;
      _creditsStart = (data['credits_start'] as num?)?.toDouble() ?? -1;
      _nextEpisodeId = data['next_episode_id'] as int?;
      _opensubtitlesConfigured = caps['opensubtitles_configured'] == true;

      _audioTracks = (data['audio_tracks'] as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();
      _subtitleTracks = (data['subtitle_tracks'] as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();

      final rawRes = caps['available_resolutions'] as List?;
      if (rawRes != null && rawRes.isNotEmpty) {
        _availableResolutions = rawRes.map((e) => e.toString()).toList();
      }

      final defaultAudioLang = (prefs['default_audio_lang'] ?? 'eng').toString().toLowerCase();
      final audioMatchIdx = _audioTracks.indexWhere(
        (t) => (t['language'] ?? '').toString().toLowerCase() == defaultAudioLang,
      );
      if (audioMatchIdx >= 0) {
        _selectedAudioIndex = _audioTracks[audioMatchIdx]['index'] as int? ?? audioMatchIdx;
      }

      final defaultSubLang = (prefs['default_subtitle_lang'] ?? 'off').toString().toLowerCase();
      if (defaultSubLang != 'off' && _subtitleTracks.isNotEmpty) {
        final subIdx = _subtitleTracks.indexWhere(
          (s) => (s['language'] ?? '').toString().toLowerCase() == defaultSubLang && s['is_text'] == true,
        );
        if (subIdx >= 0) {
          _selectedSubTrackIndex = subIdx;
        }
      }

      final directSupported = caps['direct_play_supported'] == true;
      _selectedQuality = directSupported ? 'original' : (_availableResolutions.contains('1080') ? '1080' : 'original');

      final resumeSec = widget.restart ? 0.0 : ((data['resume_position_seconds'] as num?)?.toDouble() ?? 0.0);
      await _initVideoController(seekToSeconds: resumeSec);
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = formatApiError(e, fallback: 'Unable to start playback.');
          _isLoading = false;
        });
      }
    }
  }

  String _buildPlaybackUri(String serverUrl, String token) {
    if (_selectedQuality == 'original' && _selectedAudioIndex == 0) {
      _usedHls = false;
      return '$serverUrl$_streamUrl?token=$token';
    }
    _usedHls = true;
    if (_selectedQuality == 'original') {
      return '$serverUrl$_hlsMasterUrl?audio_track=$_selectedAudioIndex&token=$token';
    }
    return '$serverUrl/hls/${widget.mediaType}/${widget.id}/variant_$_selectedQuality.m3u8?audio_track=$_selectedAudioIndex&token=$token';
  }

  Future<List<Subtitle>> _fetchSelectedSubtitles() async {
    if (_selectedSubTrackIndex == null ||
        _selectedSubTrackIndex! < 0 ||
        _selectedSubTrackIndex! >= _subtitleTracks.length) {
      return const [];
    }
    final track = _subtitleTracks[_selectedSubTrackIndex!];
    final subPath = track['url']?.toString();
    if (subPath == null || subPath.isEmpty) return const [];

    try {
      final dio = ref.read(apiClientProvider);
      final resp = await dio.get<String>(
        subPath,
        options: Options(responseType: ResponseType.plain, headers: {'Accept': 'text/vtt, text/plain, */*'}),
      );
      final rawText = resp.data ?? '';
      return _parseVttOrSrt(rawText);
    } catch (_) {
      return const [];
    }
  }

  List<Subtitle> _parseVttOrSrt(String raw) {
    final normalized = raw.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
    final blocks = normalized.split(RegExp(r'\n\n+'));
    final subtitles = <Subtitle>[];
    var counter = 0;

    for (final block in blocks) {
      final lines = block.trim().split('\n');
      if (lines.isEmpty) continue;

      final timeIdx = lines.indexWhere((l) => l.contains('-->'));
      if (timeIdx == -1) continue;

      final parts = lines[timeIdx].split('-->');
      if (parts.length < 2) continue;

      final start = _parseTimestamp(parts[0].trim());
      final end = _parseTimestamp(parts[1].trim().split(' ').first);
      if (start == null || end == null) continue;

      final text = lines
          .sublist(timeIdx + 1)
          .join('\n')
          .replaceAll(RegExp(r'<[^>]+>'), '')
          .trim();
      if (text.isEmpty) continue;

      subtitles.add(Subtitle(index: counter++, start: start, end: end, text: text));
    }
    return subtitles;
  }

  Duration? _parseTimestamp(String raw) {
    final clean = raw.replaceAll(',', '.').trim();
    final parts = clean.split(':');
    if (parts.length < 2 || parts.length > 3) return null;
    try {
      int hours = 0;
      int minutes = 0;
      double secFloat = 0;
      if (parts.length == 3) {
        hours = int.parse(parts[0]);
        minutes = int.parse(parts[1]);
        secFloat = double.parse(parts[2]);
      } else {
        minutes = int.parse(parts[0]);
        secFloat = double.parse(parts[1]);
      }
      final wholeSecs = secFloat.floor();
      final millis = ((secFloat - wholeSecs) * 1000).round();
      return Duration(hours: hours, minutes: minutes, seconds: wholeSecs, milliseconds: millis);
    } catch (_) {
      return null;
    }
  }

  Future<void> _initVideoController({double seekToSeconds = 0.0}) async {
    final session = ref.read(appSessionProvider);
    final serverUrl = session.serverUrl ?? '';
    final token = session.authToken ?? '';

    final prevChewie = _chewieController;
    final prevVideo = _videoPlayerController;

    setState(() {
      _isLoading = true;
      _error = null;
    });

    final playbackUrl = _buildPlaybackUri(serverUrl, token);
    final subs = await _fetchSelectedSubtitles();

    final vCtrl = VideoPlayerController.networkUrl(
      Uri.parse(playbackUrl),
      httpHeaders: {'Authorization': 'Bearer $token'},
    );

    await vCtrl.initialize();
    if (seekToSeconds > 0) {
      await vCtrl.seekTo(Duration(seconds: seekToSeconds.round()));
    }

    if (vCtrl.value.duration.inSeconds > 0) {
      _durationSeconds = vCtrl.value.duration.inSeconds.toDouble();
    }

    vCtrl.addListener(_onPlayerTick);

    final cCtrl = ChewieController(
      videoPlayerController: vCtrl,
      autoPlay: true,
      looping: false,
      aspectRatio: vCtrl.value.aspectRatio > 0 ? vCtrl.value.aspectRatio : 16 / 9,
      allowFullScreen: true,
      fullScreenByDefault: false,
      subtitle: Subtitles(subs),
      subtitleBuilder: (context, subtitle) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: AppColors.overlay,
          borderRadius: BorderRadius.circular(AppSpacing.radius),
        ),
        child: Text(
          subtitle,
          textAlign: TextAlign.center,
          style: const TextStyle(color: AppColors.text, fontSize: 15, fontWeight: FontWeight.w500),
        ),
      ),
      additionalOptions: (context) => [
        OptionItem(
          onTap: (ctx) {
            Navigator.pop(ctx);
            _showQualitySheet();
          },
          iconData: LucideIcons.slidersHorizontal,
          title: 'Quality ($_selectedQuality)',
        ),
        OptionItem(
          onTap: (ctx) {
            Navigator.pop(ctx);
            _showSubtitleSheet();
          },
          iconData: LucideIcons.subtitles,
          title: 'Subtitles',
        ),
        if (_audioTracks.length > 1)
          OptionItem(
            onTap: (ctx) {
              Navigator.pop(ctx);
              _showAudioSheet();
            },
            iconData: LucideIcons.volume2,
            title: 'Audio Track',
          ),
      ],
      deviceOrientationsOnEnterFullScreen: const [
        DeviceOrientation.landscapeLeft,
        DeviceOrientation.landscapeRight,
      ],
      deviceOrientationsAfterFullScreen: const [
        DeviceOrientation.portraitUp,
        DeviceOrientation.landscapeLeft,
        DeviceOrientation.landscapeRight,
      ],
      materialProgressColors: ChewieProgressColors(
        playedColor: AppColors.accent,
        handleColor: AppColors.accent,
        bufferedColor: Colors.white24,
        backgroundColor: Colors.white12,
      ),
    );

    prevVideo?.removeListener(_onPlayerTick);
    prevChewie?.dispose();
    await prevVideo?.dispose();

    if (!mounted) return;
    setState(() {
      _videoPlayerController = vCtrl;
      _chewieController = cCtrl;
      _isLoading = false;
    });

    _progressTimer?.cancel();
    _progressTimer = Timer.periodic(const Duration(seconds: 10), (_) => _reportProgress(stop: false));
  }

  void _onPlayerTick() {
    final vc = _videoPlayerController;
    if (vc == null || !vc.value.isInitialized || !mounted) return;
    final pos = vc.value.position.inMilliseconds / 1000.0;
    final dur = vc.value.duration.inSeconds.toDouble();
    if ((pos - _currentPosSeconds).abs() >= 0.8) {
      setState(() {
        _currentPosSeconds = pos;
        if (dur > 0) _durationSeconds = dur;
      });
    }
  }

  Future<void> _reportProgress({required bool stop}) async {
    final vCtrl = _videoPlayerController;
    if (vCtrl == null || !vCtrl.value.isInitialized) return;
    final durationSec = vCtrl.value.duration.inSeconds;
    final currentSec = vCtrl.value.position.inSeconds;
    if (durationSec <= 0) return;

    final progressPct = ((currentSec / durationSec) * 100.0).clamp(0.0, 100.0);
    final dio = ref.read(apiClientProvider);

    try {
      final endpoint = stop
          ? '/progress/${widget.mediaType}/${widget.id}/stop'
          : '/progress/${widget.mediaType}/${widget.id}';
      await dio.post(
        endpoint,
        data: FormData.fromMap({
          'progress': progressPct,
          'current_time': currentSec,
          'duration': durationSec,
        }),
      );
      if (stop && _usedHls) {
        await dio.post('/hls/${widget.mediaType}/${widget.id}/stop');
      }
    } catch (_) {}
  }

  void _showQualitySheet() {
    showModalBottomSheet(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const ListTile(title: Text('Playback Quality', style: TextStyle(fontWeight: FontWeight.bold))),
            const Divider(),
            ..._availableResolutions.map((res) {
              final label = res == 'original' ? 'Original (Direct / Master)' : '${res}p (HLS)';
              final selected = _selectedQuality == res;
              return ListTile(
                leading: Icon(
                  selected ? LucideIcons.checkCircle2 : LucideIcons.circle,
                  color: selected ? AppColors.accent : AppColors.textMuted,
                ),
                title: Text(label),
                onTap: () {
                  Navigator.pop(ctx);
                  if (_selectedQuality == res) return;
                  final pos = _videoPlayerController?.value.position.inSeconds.toDouble() ?? _currentPosSeconds;
                  setState(() => _selectedQuality = res);
                  _initVideoController(seekToSeconds: pos);
                },
              );
            }),
          ],
        ),
      ),
    );
  }

  void _showAudioSheet() {
    showModalBottomSheet(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const ListTile(title: Text('Audio Tracks', style: TextStyle(fontWeight: FontWeight.bold))),
            const Divider(),
            ..._audioTracks.asMap().entries.map((entry) {
              final track = entry.value;
              final idx = track['index'] as int? ?? entry.key;
              final label = (track['title'] ?? track['label'] ?? track['language'] ?? 'Track ${idx + 1}').toString();
              final selected = _selectedAudioIndex == idx;
              return ListTile(
                leading: Icon(
                  selected ? LucideIcons.checkCircle2 : LucideIcons.circle,
                  color: selected ? AppColors.accent : AppColors.textMuted,
                ),
                title: Text(label),
                subtitle: track['codec'] != null
                    ? Text('${track['codec']} • ${track['channels'] ?? ''}ch',
                        style: const TextStyle(color: AppColors.textMuted, fontSize: 12))
                    : null,
                onTap: () {
                  Navigator.pop(ctx);
                  if (_selectedAudioIndex == idx) return;
                  final pos = _videoPlayerController?.value.position.inSeconds.toDouble() ?? _currentPosSeconds;
                  setState(() => _selectedAudioIndex = idx);
                  _initVideoController(seekToSeconds: pos);
                },
              );
            }),
          ],
        ),
      ),
    );
  }

  void _showSubtitleSheet() {
    showModalBottomSheet(
      context: context,
      builder: (ctx) => SafeArea(
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const ListTile(title: Text('Subtitles', style: TextStyle(fontWeight: FontWeight.bold))),
              const Divider(),
              ListTile(
                leading: Icon(
                  _selectedSubTrackIndex == null ? LucideIcons.checkCircle2 : LucideIcons.circle,
                  color: _selectedSubTrackIndex == null ? AppColors.accent : AppColors.textMuted,
                ),
                title: const Text('Off'),
                onTap: () async {
                  Navigator.pop(ctx);
                  setState(() => _selectedSubTrackIndex = null);
                  _chewieController?.setSubtitle(const []);
                },
              ),
              ..._subtitleTracks.asMap().entries.map((entry) {
                final idx = entry.key;
                final sub = entry.value;
                final selected = _selectedSubTrackIndex == idx;
                return ListTile(
                  leading: Icon(
                    selected ? LucideIcons.checkCircle2 : LucideIcons.circle,
                    color: selected ? AppColors.accent : AppColors.textMuted,
                  ),
                  title: Text((sub['label'] ?? sub['title'] ?? sub['language'] ?? 'Subtitle').toString()),
                  subtitle: Text(
                    '${sub['language'] ?? 'und'} • ${(sub['format'] ?? 'vtt').toString().toUpperCase()}',
                    style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                  ),
                  onTap: () async {
                    Navigator.pop(ctx);
                    setState(() => _selectedSubTrackIndex = idx);
                    final parsed = await _fetchSelectedSubtitles();
                    _chewieController?.setSubtitle(parsed);
                  },
                );
              }),
              if (_opensubtitlesConfigured) ...[
                const Divider(),
                ListTile(
                  leading: const Icon(LucideIcons.globe, color: AppColors.accent),
                  title: const Text('Search OpenSubtitles'),
                  onTap: () {
                    Navigator.pop(ctx);
                    _openSubtitlesSearchModal();
                  },
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _openSubtitlesSearchModal() async {
    final dio = ref.read(apiClientProvider);
    final queryCtrl = TextEditingController(text: _title);
    List<Map<String, dynamic>> results = [];
    bool searching = true;
    String? errMsg;

    Future<void> runSearch(StateSetter setModalState) async {
      setModalState(() {
        searching = true;
        errMsg = null;
      });
      try {
        final resp = await dio.get(
          '/api/subtitles/search/${widget.mediaType}/${widget.id}',
          queryParameters: {'q': queryCtrl.text.trim(), 'lang': 'eng'},
        );
        results = (resp.data['results'] as List? ?? [])
            .whereType<Map>()
            .map((e) => Map<String, dynamic>.from(e))
            .toList();
      } catch (e) {
        errMsg = formatApiError(e, fallback: 'Subtitle search failed.');
      } finally {
        setModalState(() => searching = false);
      }
    }

    if (!mounted) return;
    final messenger = ScaffoldMessenger.of(context);
    await showDialog(
      context: context,
      builder: (dCtx) => StatefulBuilder(
        builder: (sbCtx, setModalState) {
          if (searching && results.isEmpty && errMsg == null) {
            Future.microtask(() => runSearch(setModalState));
          }
          return AlertDialog(
            title: const Text('OpenSubtitles Search'),
            content: SizedBox(
              width: 380,
              height: 400,
              child: Column(
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: queryCtrl,
                          decoration: const InputDecoration(labelText: 'Search Query'),
                          onSubmitted: (_) => runSearch(setModalState),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton(
                        icon: const Icon(LucideIcons.search),
                        onPressed: () => runSearch(setModalState),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  Expanded(
                    child: searching
                        ? const Center(child: CircularProgressIndicator(color: AppColors.accent))
                        : errMsg != null
                            ? Center(child: Text(errMsg!, style: const TextStyle(color: AppColors.failure)))
                            : results.isEmpty
                                ? const Center(child: Text('No subtitles found.', style: TextStyle(color: AppColors.textMuted)))
                                : ListView.separated(
                                    itemCount: results.length,
                                    separatorBuilder: (_, __) => const Divider(),
                                    itemBuilder: (c, i) {
                                      final r = results[i];
                                      return ListTile(
                                        contentPadding: EdgeInsets.zero,
                                        title: Text(
                                          (r['release'] ?? r['filename'] ?? 'Subtitle').toString(),
                                          maxLines: 2,
                                          overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(fontSize: 13),
                                        ),
                                        subtitle: Text(
                                          '${r['language'] ?? 'eng'} • ${r['download_count'] ?? 0} downloads',
                                          style: const TextStyle(fontSize: 11, color: AppColors.textMuted),
                                        ),
                                        trailing: const Icon(LucideIcons.download, color: AppColors.accent, size: 18),
                                        onTap: () async {
                                          Navigator.pop(dCtx);
                                          try {
                                            final fResp = await dio.post(
                                              '/api/subtitles/fetch/${widget.mediaType}/${widget.id}',
                                              data: {
                                                'file_id': r['file_id'],
                                                'lang': r['language'] ?? 'eng',
                                                'release': r['release'] ?? '',
                                                'hearing_impaired': r['hearing_impaired'] ?? false,
                                              },
                                            );
                                            final track = Map<String, dynamic>.from(fResp.data['track'] as Map);
                                            if (mounted) {
                                              setState(() {
                                                _subtitleTracks.add(track);
                                                _selectedSubTrackIndex = _subtitleTracks.length - 1;
                                              });
                                            }
                                            final parsed = await _fetchSelectedSubtitles();
                                            _chewieController?.setSubtitle(parsed);
                                            messenger.showSnackBar(
                                              const SnackBar(content: Text('Subtitle downloaded and applied!')),
                                            );
                                          } catch (e) {
                                            messenger.showSnackBar(
                                              SnackBar(content: Text(formatApiError(e, fallback: 'Failed to fetch subtitle.'))),
                                            );
                                          }
                                        },
                                      );
                                    },
                                  ),
                  ),
                ],
              ),
            ),
            actions: [TextButton(onPressed: () => Navigator.pop(dCtx), child: const Text('Close'))],
          );
        },
      ),
    );
  }

  @override
  void dispose() {
    _progressTimer?.cancel();
    _reportProgress(stop: true);
    _videoPlayerController?.removeListener(_onPlayerTick);
    _chewieController?.dispose();
    _videoPlayerController?.dispose();
    SystemChrome.setPreferredOrientations(const [DeviceOrientation.portraitUp]);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final showSkipIntro = !_introDismissed &&
        _introStart >= 0 &&
        _introEnd > _introStart &&
        _currentPosSeconds >= _introStart &&
        _currentPosSeconds < _introEnd;

    final nearCredits = _creditsStart > 0 && _currentPosSeconds >= _creditsStart;
    final nearEnd = _durationSeconds > 60 && _currentPosSeconds >= _durationSeconds - 25;
    final showNextEpisode = !_nextEpisodeDismissed &&
        widget.mediaType == 'tv' &&
        _nextEpisodeId != null &&
        (nearCredits || nearEnd);

    return Scaffold(
      backgroundColor: AppColors.bg,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(_title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 17)),
            if (_subtitleLine != null)
              Text(_subtitleLine!, style: const TextStyle(fontSize: 12, color: AppColors.textMuted)),
          ],
        ),
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => Navigator.of(context).pop(),
        ),
        actions: [
          if (!_isLoading && _error == null) ...[
            IconButton(
              icon: const Icon(LucideIcons.subtitles),
              tooltip: 'Subtitles',
              onPressed: _showSubtitleSheet,
            ),
            if (_audioTracks.length > 1)
              IconButton(
                icon: const Icon(LucideIcons.volume2),
                tooltip: 'Audio Track',
                onPressed: _showAudioSheet,
              ),
            IconButton(
              icon: const Icon(LucideIcons.slidersHorizontal),
              tooltip: 'Quality',
              onPressed: _showQualitySheet,
            ),
          ],
        ],
      ),
      body: Stack(
        children: [
          Center(
            child: _isLoading
                ? const CircularProgressIndicator(color: AppColors.accent)
                : _error != null
                    ? Padding(
                        padding: const EdgeInsets.all(24.0),
                        child: Text(_error!, style: const TextStyle(color: AppColors.failure), textAlign: TextAlign.center),
                      )
                    : _chewieController != null
                        ? Chewie(controller: _chewieController!)
                        : const SizedBox.shrink(),
          ),
          if (showSkipIntro)
            Positioned(
              right: 20,
              bottom: 76,
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: AppColors.overlay,
                  side: const BorderSide(color: AppColors.textMuted),
                ),
                onPressed: () {
                  _introDismissed = true;
                  _videoPlayerController?.seekTo(Duration(seconds: _introEnd.round()));
                },
                icon: const Icon(LucideIcons.skipForward, size: 16),
                label: const Text('Skip Intro'),
              ),
            ),
          if (showNextEpisode)
            Positioned(
              right: 20,
              bottom: 76,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  ElevatedButton.icon(
                    onPressed: () => context.pushReplacement('/play/tv/$_nextEpisodeId'),
                    icon: const Icon(LucideIcons.skipForward, size: 16),
                    label: const Text('Next Episode'),
                  ),
                  const SizedBox(width: 6),
                  IconButton(
                    style: IconButton.styleFrom(backgroundColor: AppColors.overlay),
                    icon: const Icon(LucideIcons.x, size: 16, color: AppColors.text),
                    onPressed: () => setState(() => _nextEpisodeDismissed = true),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
