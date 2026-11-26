import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import 'audio_player_controller.dart';

class AlbumScreen extends ConsumerStatefulWidget {
  final String albumId;

  const AlbumScreen({super.key, required this.albumId});

  @override
  ConsumerState<AlbumScreen> createState() => _AlbumScreenState();
}

class _AlbumScreenState extends ConsumerState<AlbumScreen> {
  Map<String, dynamic>? _album;
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
      final resp = await dio.get('/api/music/album/${widget.albumId}/tracks');
      final data = Map<String, dynamic>.from(resp.data as Map);
      setState(() {
        _album = data['album'] is Map ? Map<String, dynamic>.from(data['album'] as Map) : null;
        _tracks = (data['tracks'] as List? ?? [])
            .whereType<Map>()
            .map((t) => Map<String, dynamic>.from(t))
            .toList();
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _error = formatApiError(e, fallback: 'Unable to load album.');
        _loading = false;
      });
    }
  }

  List<AudioTrackItem> _asAudioItems() =>
      _tracks.map((t) => AudioTrackItem.fromJson(t)).toList();

  Future<void> _addToPlaylist(int trackId) async {
    final dio = ref.read(apiClientProvider);
    try {
      final resp = await dio.get('/api/playlists');
      final playlists = (resp.data as List? ?? [])
          .whereType<Map>()
          .map((p) => Map<String, dynamic>.from(p))
          .toList();
      if (!mounted) return;
      final selectedId = await showDialog<int>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Add to Playlist'),
          content: SizedBox(
            width: 300,
            child: playlists.isEmpty
                ? const Text('No playlists created yet. Create one in Music > Playlists.')
                : ListView.builder(
                    shrinkWrap: true,
                    itemCount: playlists.length,
                    itemBuilder: (c, i) {
                      final pl = playlists[i];
                      return ListTile(
                        title: Text(pl['name']?.toString() ?? 'Playlist'),
                        subtitle: Text('${pl['track_count'] ?? 0} tracks'),
                        onTap: () => Navigator.pop(ctx, pl['id'] as int),
                      );
                    },
                  ),
          ),
          actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Close'))],
        ),
      );
      if (selectedId != null) {
        await dio.post('/api/playlists/$selectedId/tracks', data: {'track_id': trackId});
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Added to playlist')));
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(formatApiError(e))));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final serverUrl = ref.watch(serverUrlProvider);
    final album = _album ?? const {};
    final coverUrl = buildMetadataImageUrl(serverUrl, album['cover_url'] as String?);
    final artistId = album['artist_id'];

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        title: Text(album['title']?.toString() ?? 'Album'),
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
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.end,
                        children: [
                          ClipRRect(
                            borderRadius: BorderRadius.circular(AppSpacing.radius),
                            child: Container(
                              width: 132,
                              height: 132,
                              color: AppColors.surface,
                              child: coverUrl != null
                                  ? CachedNetworkImage(imageUrl: coverUrl, fit: BoxFit.cover)
                                  : const Icon(LucideIcons.disc, size: 48, color: AppColors.textMuted),
                            ),
                          ),
                          const SizedBox(width: 16),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  album['title']?.toString() ?? 'Album',
                                  style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: AppColors.text),
                                ),
                                const SizedBox(height: 4),
                                GestureDetector(
                                  onTap: artistId != null ? () => context.push('/artist/$artistId') : null,
                                  child: Text(
                                    album['artist_name']?.toString() ?? '',
                                    style: TextStyle(
                                      color: artistId != null ? AppColors.accent : AppColors.textMuted,
                                      fontSize: 15,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                ),
                                if (album['year'] != null)
                                  Padding(
                                    padding: const EdgeInsets.only(top: 4),
                                    child: Text(
                                      '${album['year']}${album['genre'] != null ? ' • ${album['genre']}' : ''}',
                                      style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                                    ),
                                  ),
                              ],
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 16),
                      Row(
                        children: [
                          Expanded(
                            child: ElevatedButton.icon(
                              onPressed: _tracks.isEmpty
                                  ? null
                                  : () => ref.read(audioPlayerProvider.notifier).playQueue(_asAudioItems()),
                              icon: const Icon(LucideIcons.play),
                              label: const Text('Play All'),
                            ),
                          ),
                          const SizedBox(width: 10),
                          OutlinedButton.icon(
                            onPressed: _tracks.isEmpty
                                ? null
                                : () {
                                    final shuffled = List<AudioTrackItem>.from(_asAudioItems())..shuffle();
                                    ref.read(audioPlayerProvider.notifier).playQueue(shuffled);
                                  },
                            icon: const Icon(LucideIcons.shuffle),
                            label: const Text('Shuffle'),
                          ),
                        ],
                      ),
                      const SizedBox(height: 18),
                      const Divider(),
                      ..._tracks.asMap().entries.map((entry) {
                        final idx = entry.key;
                        final t = entry.value;
                        return ListTile(
                          contentPadding: const EdgeInsets.symmetric(horizontal: 4),
                          leading: SizedBox(
                            width: 28,
                            child: Center(
                              child: Text(
                                '${t['track_number'] ?? idx + 1}',
                                style: const TextStyle(color: AppColors.textMuted),
                              ),
                            ),
                          ),
                          title: Text(
                            t['title']?.toString() ?? 'Track',
                            style: const TextStyle(color: AppColors.text, fontWeight: FontWeight.w500),
                          ),
                          subtitle: Text(
                            t['duration_str']?.toString() ?? '',
                            style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
                          ),
                          trailing: IconButton(
                            icon: const Icon(LucideIcons.listPlus, size: 18, color: AppColors.textMuted),
                            onPressed: () => _addToPlaylist(t['id'] as int),
                          ),
                          onTap: () => ref
                              .read(audioPlayerProvider.notifier)
                              .playQueue(_asAudioItems(), startIndex: idx),
                        );
                      }),
                    ],
                  ),
                ),
    );
  }
}
