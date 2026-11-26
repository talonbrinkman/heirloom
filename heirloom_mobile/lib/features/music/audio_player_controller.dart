import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:just_audio/just_audio.dart';
import '../../core/api_client.dart';

class AudioTrackItem {
  final int id;
  final String title;
  final String artistName;
  final String? albumTitle;
  final int? albumId;
  final String? coverUrl;
  final String streamUrl;
  final double durationSeconds;

  const AudioTrackItem({
    required this.id,
    required this.title,
    required this.artistName,
    this.albumTitle,
    this.albumId,
    this.coverUrl,
    required this.streamUrl,
    this.durationSeconds = 0,
  });

  factory AudioTrackItem.fromJson(Map<String, dynamic> json) {
    final cover = json['cover_url'] as String? ??
        (json['poster_filename'] != null ? '/metadata/${json['poster_filename']}' : null);
    final rawStream = json['stream_url'] as String? ?? '/stream/music/${json['id']}';
    return AudioTrackItem(
      id: json['id'] is int ? json['id'] as int : int.tryParse('${json['id']}') ?? 0,
      title: (json['title'] ?? 'Unknown Track').toString(),
      artistName: (json['artist_name'] ?? 'Unknown Artist').toString(),
      albumTitle: json['album_title']?.toString(),
      albumId: json['album_id'] is int ? json['album_id'] as int : int.tryParse('${json['album_id']}'),
      coverUrl: cover,
      streamUrl: rawStream,
      durationSeconds: (json['duration'] as num?)?.toDouble() ?? 0.0,
    );
  }
}

class AudioPlayerState {
  final List<AudioTrackItem> queue;
  final int currentIndex;
  final bool isPlaying;
  final bool isLoading;
  final bool shuffleEnabled;
  final LoopMode loopMode;
  final Duration position;
  final Duration duration;

  const AudioPlayerState({
    this.queue = const [],
    this.currentIndex = -1,
    this.isPlaying = false,
    this.isLoading = false,
    this.shuffleEnabled = false,
    this.loopMode = LoopMode.off,
    this.position = Duration.zero,
    this.duration = Duration.zero,
  });

  AudioTrackItem? get currentTrack =>
      (currentIndex >= 0 && currentIndex < queue.length) ? queue[currentIndex] : null;

  bool get hasTrack => currentTrack != null;

  AudioPlayerState copyWith({
    List<AudioTrackItem>? queue,
    int? currentIndex,
    bool? isPlaying,
    bool? isLoading,
    bool? shuffleEnabled,
    LoopMode? loopMode,
    Duration? position,
    Duration? duration,
  }) {
    return AudioPlayerState(
      queue: queue ?? this.queue,
      currentIndex: currentIndex ?? this.currentIndex,
      isPlaying: isPlaying ?? this.isPlaying,
      isLoading: isLoading ?? this.isLoading,
      shuffleEnabled: shuffleEnabled ?? this.shuffleEnabled,
      loopMode: loopMode ?? this.loopMode,
      position: position ?? this.position,
      duration: duration ?? this.duration,
    );
  }
}

class AudioPlayerController extends StateNotifier<AudioPlayerState> {
  final Ref _ref;
  final AudioPlayer _player = AudioPlayer();

  AudioPlayerController(this._ref) : super(const AudioPlayerState()) {
    _player.playerStateStream.listen((ps) {
      if (!mounted) return;
      final isLoading = ps.processingState == ProcessingState.loading ||
          ps.processingState == ProcessingState.buffering;
      state = state.copyWith(isPlaying: ps.playing, isLoading: isLoading);

      if (ps.processingState == ProcessingState.completed) {
        if (state.loopMode == LoopMode.one) {
          _player.seek(Duration.zero);
          _player.play();
        } else {
          next();
        }
      }
    });

    _player.positionStream.listen((pos) {
      if (mounted) state = state.copyWith(position: pos);
    });

    _player.durationStream.listen((dur) {
      if (mounted && dur != null) state = state.copyWith(duration: dur);
    });
  }

  Future<void> playQueue(List<AudioTrackItem> tracks, {int startIndex = 0}) async {
    if (tracks.isEmpty) return;
    final idx = startIndex.clamp(0, tracks.length - 1);
    state = state.copyWith(queue: List.unmodifiable(tracks), currentIndex: idx, isLoading: true);
    await _loadAndPlayIndex(idx);
  }

  Future<void> playSingleTrackById(int trackId) async {
    final dio = _ref.read(apiClientProvider);
    final resp = await dio.get('/api/music/track/$trackId');
    final track = AudioTrackItem.fromJson(Map<String, dynamic>.from(resp.data as Map));
    await playQueue([track], startIndex: 0);
  }

  Future<void> _loadAndPlayIndex(int index) async {
    if (index < 0 || index >= state.queue.length) return;
    final track = state.queue[index];
    final session = _ref.read(appSessionProvider);
    final baseUrl = session.serverUrl ?? '';
    final token = session.authToken ?? '';
    if (baseUrl.isEmpty) return;

    final rawPath = track.streamUrl.startsWith('/') ? track.streamUrl : '/${track.streamUrl}';
    final separator = rawPath.contains('?') ? '&' : '?';
    final uri = Uri.parse('$baseUrl$rawPath${token.isNotEmpty ? '${separator}token=$token' : ''}');

    try {
      await _player.setAudioSource(
        AudioSource.uri(
          uri,
          headers: token.isNotEmpty ? {'Authorization': 'Bearer $token'} : null,
        ),
      );
      await _player.play();
    } catch (_) {
      if (mounted) state = state.copyWith(isLoading: false);
    }
  }

  Future<void> togglePlayPause() async {
    if (_player.playing) {
      await _player.pause();
    } else {
      await _player.play();
    }
  }

  Future<void> seek(Duration position) => _player.seek(position);

  Future<void> next() async {
    if (state.queue.isEmpty) return;
    int nextIdx;
    if (state.shuffleEnabled && state.queue.length > 1) {
      nextIdx = (state.currentIndex + 1 + DateTime.now().millisecond % (state.queue.length - 1)) % state.queue.length;
    } else {
      nextIdx = state.currentIndex + 1;
      if (nextIdx >= state.queue.length) {
        if (state.loopMode == LoopMode.all) {
          nextIdx = 0;
        } else {
          await _player.pause();
          await _player.seek(Duration.zero);
          return;
        }
      }
    }
    state = state.copyWith(currentIndex: nextIdx, position: Duration.zero);
    await _loadAndPlayIndex(nextIdx);
  }

  Future<void> previous() async {
    if (state.queue.isEmpty) return;
    if (state.position.inSeconds > 3) {
      await _player.seek(Duration.zero);
      return;
    }
    final prevIdx = state.currentIndex > 0
        ? state.currentIndex - 1
        : (state.loopMode == LoopMode.all ? state.queue.length - 1 : 0);
    state = state.copyWith(currentIndex: prevIdx, position: Duration.zero);
    await _loadAndPlayIndex(prevIdx);
  }

  void toggleShuffle() {
    state = state.copyWith(shuffleEnabled: !state.shuffleEnabled);
  }

  void cycleLoopMode() {
    final nextMode = switch (state.loopMode) {
      LoopMode.off => LoopMode.all,
      LoopMode.all => LoopMode.one,
      LoopMode.one => LoopMode.off,
    };
    state = state.copyWith(loopMode: nextMode);
  }

  Future<void> stop() async {
    await _player.stop();
    state = const AudioPlayerState();
  }

  @override
  void dispose() {
    _player.dispose();
    super.dispose();
  }
}

final audioPlayerProvider = StateNotifierProvider<AudioPlayerController, AudioPlayerState>((ref) {
  return AudioPlayerController(ref);
});
