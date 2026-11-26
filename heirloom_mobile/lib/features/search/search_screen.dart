import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets/media_card.dart';
import '../media/photo_viewer_dialog.dart';
import '../music/audio_player_controller.dart';

final searchQueryProvider = StateProvider<String>((ref) => '');

final searchResultsProvider = FutureProvider.family<List<dynamic>, String>((ref, query) async {
  if (query.trim().isEmpty) return [];
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/search', queryParameters: {'q': query.trim()});
  return response.data['items'] as List<dynamic>;
});

class SearchScreen extends ConsumerStatefulWidget {
  const SearchScreen({super.key});

  @override
  ConsumerState<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends ConsumerState<SearchScreen> {
  final TextEditingController _controller = TextEditingController();
  Timer? _debounce;
  bool _loomMode = false;
  bool _loomLoading = false;
  String? _loomError;
  List<Map<String, dynamic>> _loomResults = [];

  @override
  void dispose() {
    _controller.dispose();
    _debounce?.cancel();
    super.dispose();
  }

  void _onSearchChanged(String query) {
    setState(() {});
    if (_loomMode) return;
    if (_debounce?.isActive ?? false) _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 450), () {
      ref.read(searchQueryProvider.notifier).state = query;
    });
  }

  Future<void> _searchLoom() async {
    final q = _controller.text.trim();
    if (q.length < 2) {
      setState(() => _loomError = 'Enter at least 2 characters to search The Loom.');
      return;
    }
    setState(() {
      _loomLoading = true;
      _loomError = null;
      _loomResults = [];
    });
    try {
      final dio = ref.read(apiClientProvider);
      final resp = await dio.post('/api/loom/search', data: {'query': q});
      final data = Map<String, dynamic>.from(resp.data as Map);
      final combined = <Map<String, dynamic>>[];
      for (final kind in ['movies', 'tv', 'music', 'photos']) {
        final list = data[kind] as List? ?? [];
        for (final entry in list) {
          if (entry is Map) {
            combined.add({
              ...Map<String, dynamic>.from(entry),
              'media_type': kind == 'movies' ? 'movie' : (kind == 'photos' ? 'photo' : kind),
            });
          }
        }
      }
      if (!mounted) return;
      setState(() {
        _loomResults = combined;
        _loomLoading = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _loomError = formatApiError(e, fallback: 'Loom search failed.');
          _loomLoading = false;
        });
      }
    }
  }

  Future<void> _queueLoomSync(Map<String, dynamic> item) async {
    try {
      final dio = ref.read(apiClientProvider);
      await dio.post('/api/loom/sync/queue', data: {
        'peer_id': item['peer_id'],
        'items': [item],
      });
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Queued Loom download: ${item['title']}')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(formatApiError(e, fallback: 'Could not queue download.'))),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final query = ref.watch(searchQueryProvider);
    final resultsAsync = ref.watch(searchResultsProvider(query));
    final session = ref.watch(appSessionProvider);
    final serverUrl = session.serverUrl ?? '';
    final isAdmin = session.isAdmin;

    return Scaffold(
      appBar: AppBar(
        title: TextField(
          controller: _controller,
          autofocus: true,
          decoration: InputDecoration(
            hintText: _loomMode ? 'Search connected Loom peers...' : 'Search movies, TV, music, photos...',
            border: InputBorder.none,
            enabledBorder: InputBorder.none,
            focusedBorder: InputBorder.none,
            filled: false,
            hintStyle: const TextStyle(color: AppColors.textMuted),
          ),
          style: const TextStyle(color: AppColors.text, fontSize: 16),
          onChanged: _onSearchChanged,
          onSubmitted: (_) {
            if (_loomMode) _searchLoom();
          },
        ),
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        actions: [
          if (_loomMode)
            IconButton(
              icon: const Icon(LucideIcons.search, color: AppColors.accent),
              onPressed: _searchLoom,
            ),
          if (_controller.text.isNotEmpty)
            IconButton(
              icon: const Icon(LucideIcons.x),
              onPressed: () {
                _controller.clear();
                _onSearchChanged('');
              },
            ),
        ],
        bottom: isAdmin
            ? PreferredSize(
                preferredSize: const Size.fromHeight(44),
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Row(
                    children: [
                      ChoiceChip(
                        label: const Text('Local Library'),
                        selected: !_loomMode,
                        selectedColor: AppColors.accent,
                        onSelected: (v) {
                          if (v) setState(() => _loomMode = false);
                        },
                      ),
                      const SizedBox(width: 8),
                      ChoiceChip(
                        label: const Text('Search The Loom'),
                        selected: _loomMode,
                        selectedColor: AppColors.accent,
                        onSelected: (v) {
                          if (v) setState(() => _loomMode = true);
                        },
                      ),
                    ],
                  ),
                ),
              )
            : null,
      ),
      body: _loomMode
          ? _buildLoomBody()
          : query.isEmpty
              ? const Center(child: Text('Type to start searching', style: TextStyle(color: AppColors.textMuted)))
              : resultsAsync.when(
                  loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
                  error: (err, stack) => Center(child: Text(formatApiError(err, fallback: 'Search failed.'), style: const TextStyle(color: AppColors.failure))),
                  data: (items) {
                    if (items.isEmpty) {
                      return const Center(child: Text('No results found', style: TextStyle(color: AppColors.textMuted)));
                    }
                    return GridView.builder(
                      padding: const EdgeInsets.all(14),
                      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                        crossAxisCount: 3,
                        childAspectRatio: 0.56,
                        crossAxisSpacing: 10,
                        mainAxisSpacing: 12,
                      ),
                      itemCount: items.length,
                      itemBuilder: (context, index) {
                        final item = Map<String, dynamic>.from(items[index] as Map);
                        final posterUrl = buildMetadataImageUrl(
                          serverUrl,
                          (item['poster_filename'] ?? item['poster'])?.toString(),
                        );
                        final url = item['url'] as String? ?? '';
                        final parts = url.split('/').where((p) => p.isNotEmpty).toList();
                        final itemType = item['type'] as String? ?? (parts.isNotEmpty ? parts[0] : 'movie');

                        return MediaCard(
                          itemId: item['id'],
                          title: item['title']?.toString() ?? '',
                          subtitle: item['year']?.toString(),
                          imageUrl: posterUrl,
                          mediaType: itemType,
                          showActionMenu: itemType == 'movie' || itemType == 'tv',
                          onMutated: () => ref.invalidate(searchResultsProvider(query)),
                          onTap: () {
                            if (itemType == 'photo' || (parts.isNotEmpty && parts[0] == 'photo')) {
                              showHeirloomPhotoViewer(context, item, serverUrl);
                            } else if (parts.isNotEmpty && parts[0] == 'album') {
                              context.push('/album/${parts[1]}');
                            } else if (parts.isNotEmpty && parts[0] == 'artist') {
                              context.push('/artist/${parts[1]}');
                            } else if (itemType == 'music' || itemType == 'track') {
                              final tid = int.tryParse('${item['id']}');
                              if (tid != null) {
                                ref.read(audioPlayerProvider.notifier).playSingleTrackById(tid);
                              }
                            } else if (parts.length >= 2) {
                              final type = parts[0] == 'series' ? 'tv' : parts[0];
                              final id = Uri.decodeComponent(parts[1]);
                              context.push('/details/$type/$id');
                            } else if (item['id'] != null) {
                              final type = itemType == 'tv' ? 'tv' : 'movie';
                              context.push('/details/$type/${item['id']}');
                            }
                          },
                        );
                      },
                    );
                  },
                ),
    );
  }

  Widget _buildLoomBody() {
    if (_loomLoading) {
      return const Center(child: CircularProgressIndicator(color: AppColors.accent));
    }
    if (_loomError != null) {
      return Center(child: Text(_loomError!, style: const TextStyle(color: AppColors.failure)));
    }
    if (_loomResults.isEmpty) {
      return const Center(
        child: Text(
          'Enter a title and tap Search to query connected Loom peers.',
          style: TextStyle(color: AppColors.textMuted),
          textAlign: TextAlign.center,
        ),
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.all(14),
      itemCount: _loomResults.length,
      separatorBuilder: (_, __) => const Divider(),
      itemBuilder: (ctx, idx) {
        final r = _loomResults[idx];
        return ListTile(
          title: Text(r['title']?.toString() ?? 'Unknown'),
          subtitle: Text(
            '${r['peer_name'] ?? 'Peer'} • ${r['quality_label'] ?? ''} • ${r['media_type'] ?? ''}',
            style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
          ),
          trailing: ElevatedButton.icon(
            onPressed: () => _queueLoomSync(r),
            icon: const Icon(LucideIcons.download, size: 15),
            label: const Text('Sync'),
          ),
        );
      },
    );
  }
}
