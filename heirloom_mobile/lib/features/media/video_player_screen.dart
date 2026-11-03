import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';
import 'package:chewie/chewie.dart';
import 'package:dio/dio.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

class VideoPlayerScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String id;

  const VideoPlayerScreen({
    super.key,
    required this.mediaType,
    required this.id,
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
  String? _title;

  @override
  void initState() {
    super.initState();
    _initializePlayer();
  }

  Future<void> _initializePlayer() async {
    final serverUrl = ref.read(serverUrlProvider);
    final authToken = ref.read(authTokenProvider);
    final dio = ref.read(apiClientProvider);

    if (serverUrl == null || authToken == null) {
      setState(() {
        _error = 'Not authenticated or server URL is missing.';
        _isLoading = false;
      });
      return;
    }

    try {
      // 1. Fetch playback info from Heirloom API
      final res = await dio.get('/api/playback/info/${widget.mediaType}/${widget.id}');
      final data = res.data as Map<String, dynamic>;

      _title = data['title'] ?? 'Heirloom Video';
      final streamPath = data['stream_url'] as String? ?? '/stream/${widget.mediaType}/${widget.id}';
      final resumeSeconds = (data['resume_position_seconds'] as num?)?.toDouble() ?? 0.0;

      final fullStreamUrl = '$serverUrl$streamPath?token=$authToken';
      final videoUri = Uri.parse(fullStreamUrl);

      // 2. Initialize native video player controller
      final vController = VideoPlayerController.networkUrl(
        videoUri,
        httpHeaders: {'Authorization': 'Bearer $authToken'},
      );
      await vController.initialize();

      // 3. Resume if playback position is stored
      if (resumeSeconds > 0) {
        await vController.seekTo(Duration(seconds: resumeSeconds.toInt()));
      }

      // 4. Wrap with Chewie HUD
      final cController = ChewieController(
        videoPlayerController: vController,
        autoPlay: true,
        looping: false,
        aspectRatio: vController.value.aspectRatio > 0 ? vController.value.aspectRatio : 16 / 9,
        allowFullScreen: true,
        fullScreenByDefault: false,
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
          playedColor: AppTheme.primaryRed,
          handleColor: AppTheme.primaryRed,
          bufferedColor: Colors.white24,
          backgroundColor: Colors.white12,
        ),
      );

      if (mounted) {
        setState(() {
          _videoPlayerController = vController;
          _chewieController = cController;
          _isLoading = false;
        });

        // 5. Start periodic progress reporting every 10 seconds
        _startProgressTimer();
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Failed to load video: $e';
          _isLoading = false;
        });
      }
    }
  }

  void _startProgressTimer() {
    _progressTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      _reportProgress();
    });
  }

  Future<void> _reportProgress() async {
    final vController = _videoPlayerController;
    if (vController == null || !vController.value.isInitialized) return;

    final durationSec = vController.value.duration.inSeconds;
    final currentSec = vController.value.position.inSeconds;

    if (durationSec > 0) {
      final progressPercent = (currentSec / durationSec) * 100.0;
      final dio = ref.read(apiClientProvider);

      try {
        await dio.post(
          '/progress/${widget.mediaType}/${widget.id}',
          data: FormData.fromMap({
            'progress': progressPercent,
            'current_time': currentSec,
            'duration': durationSec,
          }),
        );
      } catch (_) {
        // Silent catch for network drops during playback
      }
    }
  }

  @override
  void dispose() {
    _progressTimer?.cancel();
    _reportProgress();
    _chewieController?.dispose();
    _videoPlayerController?.dispose();
    SystemChrome.setPreferredOrientations(const [
      DeviceOrientation.portraitUp,
    ]);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: Text(_title ?? ''),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.of(context).pop(),
        ),
      ),
      body: Center(
        child: _isLoading
            ? const CircularProgressIndicator(color: AppTheme.primaryRed)
            : _error != null
                ? Padding(
                    padding: const EdgeInsets.all(24.0),
                    child: Text(_error!, style: const TextStyle(color: Colors.red), textAlign: TextAlign.center),
                  )
                : _chewieController != null
                    ? Chewie(controller: _chewieController!)
                    : const SizedBox.shrink(),
      ),
    );
  }
}
