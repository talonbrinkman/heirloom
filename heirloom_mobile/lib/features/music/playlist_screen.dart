import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import 'audio_player_controller.dart';

class PlaylistScreen extends ConsumerStatefulWidget {
  final String playlistId;

  const PlaylistScreen({super.key, required this.playlistId});

  @override
  ConsumerState<PlaylistScreen> createState() => _PlaylistScreenState();
}

class _PlaylistScreenState extends ConsumerState<PlaylistScreen> {
  String _name = 'Playlist';
  List<Map<String, dynamic>> _tracks = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final dio = ref.read(apiClientProvider);
      final resp = await dio.get('/api/playlists/${widget.playlistId}');
      final data = Map<String, dynamic>.from(resp.data as Map);
      setState(() {
        _name = data['name']?.toString() ?? 'Playlist';
        _tracks = (data['tracks'] as List? ?? [])
            .whereType<Map>()
            .map((e) => Map<String, dynamic>.from(e))
            .toList();
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _error = formatApiError(e, fallback: 'Unable to load playlist.');
        _loading = false;
      });
    }
  }

  Future<void> _removeTrack(int trackId) async {
    final dio = ref.read(apiClientProvider);
    await dio.delete('/api/playlists/${widget.playlistId}/tracks/$trackId');
    await _load();
  }

  Future<void> _deletePlaylist() async {
    final dio = ref.read(apiClientProvider);
    await dio.delete('/api/playlists/${widget.playlistId}');
    if (mounted) context.pop();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        title: Text(_name),
        actions: [
          IconButton(
            icon: const Icon(LucideIcons.trash2, color: AppColors.failure),
            onPressed: _deletePlaylist,
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: AppColors.accent))
          : _error != null
              ? Center(child: Text(_error!, style: const TextStyle(color: AppColors.failure)))
              : RefreshIndicator(
                  onRefresh: _load,
                  child: ListView(
                    padding: const EdgeInsets.all(16),
                    children: [
                      ElevatedButton.icon(
                        onPressed: _tracks.isEmpty
                            ? null
                            : () => ref
                                .read(audioPlayerProvider.notifier)
                                .playQueue(_tracks.map((t) => AudioTrackItem.fromJson(t)).toList()),
                        icon: const Icon(LucideIcons.play),
                        label: Text('Play All (${_tracks.length} tracks)'),
                      ),
                      const SizedBox(height: 14),
                      if (_tracks.isEmpty)
                        const Padding(
                          padding: EdgeInsets.only(top: 32),
                          child: Center(
                            child: Text('This playlist has no tracks yet.', style: TextStyle(color: AppColors.textMuted)),
                          ),
                        ),
                      ..._tracks.asMap().entries.map((entry) {
                        final idx = entry.key;
                        final t = entry.value;
                        return ListTile(
                          leading: const Icon(LucideIcons.music, color: AppColors.textMuted),
                          title: Text(t['title']?.toString() ?? 'Track'),
                          subtitle: Text(
                            '${t['artist_name'] ?? ''} • ${t['duration_str'] ?? ''}',
                            style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                          ),
                          trailing: IconButton(
                            icon: const Icon(LucideIcons.x, size: 18, color: AppColors.textMuted),
                            onPressed: () => _removeTrack(t['id'] as int),
                          ),
                          onTap: () => ref.read(audioPlayerProvider.notifier).playQueue(
                                _tracks.map((m) => AudioTrackItem.fromJson(m)).toList(),
                                startIndex: idx,
                              ),
                        );
                      }),
                    ],
                  ),
                ),
    );
  }
}
