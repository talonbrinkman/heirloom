import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import 'audio_player_controller.dart';

class ArtistScreen extends ConsumerStatefulWidget {
  final String artistId;

  const ArtistScreen({super.key, required this.artistId});

  @override
  ConsumerState<ArtistScreen> createState() => _ArtistScreenState();
}

class _ArtistScreenState extends ConsumerState<ArtistScreen> {
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
      final resp = await dio.get('/api/music/artist/${widget.artistId}/tracks');
      final list = (resp.data as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();
      setState(() {
        _tracks = list;
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _error = formatApiError(e, fallback: 'Unable to load artist.');
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final artistName = _tracks.isNotEmpty ? (_tracks.first['artist_name']?.toString() ?? 'Artist') : 'Artist';

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        title: Text(artistName),
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
                      if (_tracks.isNotEmpty)
                        ElevatedButton.icon(
                          onPressed: () => ref
                              .read(audioPlayerProvider.notifier)
                              .playQueue(_tracks.map((t) => AudioTrackItem.fromJson(t)).toList()),
                          icon: const Icon(LucideIcons.play),
                          label: const Text('Play Artist Tracks'),
                        ),
                      const SizedBox(height: 16),
                      ..._tracks.asMap().entries.map((entry) {
                        final idx = entry.key;
                        final t = entry.value;
                        return ListTile(
                          leading: const Icon(LucideIcons.music, color: AppColors.textMuted),
                          title: Text(t['title']?.toString() ?? 'Track'),
                          subtitle: Text(
                            '${t['album_title'] ?? ''} • ${t['duration_str'] ?? ''}',
                            style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                          ),
                          trailing: t['album_id'] != null
                              ? IconButton(
                                  icon: const Icon(LucideIcons.disc, size: 18, color: AppColors.textMuted),
                                  onPressed: () => context.push('/album/${t['album_id']}'),
                                )
                              : null,
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
