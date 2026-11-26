import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets/media_card.dart';
import '../../core/widgets/profile_avatar_button.dart';
import '../media/photo_viewer_dialog.dart';
import '../music/audio_player_controller.dart';

final photoRecommendedProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/photos/recommended');
  final sections = response.data['sections'] as List<dynamic>? ?? [];
  return sections.map((s) => Map<String, dynamic>.from(s as Map)).toList();
});

final photoTimelineProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/photos/timeline', queryParameters: {'page': 1, 'limit': 120});
  final items = response.data['items'] as List<dynamic>? ?? [];
  return items.map((s) => Map<String, dynamic>.from(s as Map)).toList();
});

class LibraryScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String title;

  const LibraryScreen({super.key, required this.mediaType, required this.title});

  @override
  ConsumerState<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends ConsumerState<LibraryScreen> {
  static const int _pageSize = 42;
  final ScrollController _scrollController = ScrollController();

  String _sort = 'title_asc';
  String _subTab = 'library'; // library, recommended, categories, collections (or timeline / albums/artists/tracks/playlists)
  String? _activeGenreFilter;
  String? _activeCollectionFilter;
  String? _currentPhotoAlbum;
  final List<String?> _photoAlbumHistory = [];

  final List<Map<String, dynamic>> _items = [];
  int _page = 1;
  bool _isLoadingInitial = true;
  bool _isLoadingMore = false;
  bool _hasMore = true;
  String? _error;

  Map<String, dynamic>? _auxData;
  bool _auxLoading = false;
  String? _auxError;

  @override
  void initState() {
    super.initState();
    if (widget.mediaType == 'music') {
      _subTab = 'albums';
    }
    _scrollController.addListener(_onScroll);
    _loadInitial();
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (_subTab != 'library' && widget.mediaType != 'music') return;
    if (!_hasMore || _isLoadingMore || _isLoadingInitial) return;
    if (_scrollController.position.pixels >= _scrollController.position.maxScrollExtent - 400) {
      _loadMore();
    }
  }

  Future<void> _loadInitial() async {
    setState(() {
      _isLoadingInitial = true;
      _error = null;
      _page = 1;
      _hasMore = true;
      _items.clear();
    });

    try {
      final dio = ref.read(apiClientProvider);
      final params = <String, dynamic>{
        'page': 1,
        'limit': _pageSize,
        'sort': _sort,
        if (widget.mediaType == 'music') 'view': _subTab,
        if (widget.mediaType == 'photo' && _currentPhotoAlbum != null) 'album': _currentPhotoAlbum,
        if (_activeGenreFilter != null) 'genre': _activeGenreFilter,
        if (_activeCollectionFilter != null) 'collection': _activeCollectionFilter,
      };
      final resp = await dio.get('/api/library/${widget.mediaType}', queryParameters: params);
      final raw = (resp.data['items'] as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();
      if (!mounted) return;
      setState(() {
        _items.addAll(raw);
        _hasMore = raw.length >= _pageSize;
        _isLoadingInitial = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = formatApiError(e, fallback: 'Unable to load ${widget.title}.');
          _isLoadingInitial = false;
        });
      }
    }
  }

  Future<void> _loadMore() async {
    setState(() => _isLoadingMore = true);
    try {
      final nextPage = _page + 1;
      final dio = ref.read(apiClientProvider);
      final params = <String, dynamic>{
        'page': nextPage,
        'limit': _pageSize,
        'sort': _sort,
        if (widget.mediaType == 'music') 'view': _subTab,
        if (widget.mediaType == 'photo' && _currentPhotoAlbum != null) 'album': _currentPhotoAlbum,
        if (_activeGenreFilter != null) 'genre': _activeGenreFilter,
        if (_activeCollectionFilter != null) 'collection': _activeCollectionFilter,
      };
      final resp = await dio.get('/api/library/${widget.mediaType}', queryParameters: params);
      final raw = (resp.data['items'] as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList();
      if (!mounted) return;
      setState(() {
        _page = nextPage;
        _items.addAll(raw);
        _hasMore = raw.length >= _pageSize;
        _isLoadingMore = false;
      });
    } catch (_) {
      if (mounted) setState(() => _isLoadingMore = false);
    }
  }

  Future<void> _loadAuxView(String view) async {
    setState(() {
      _auxLoading = true;
      _auxError = null;
      _auxData = null;
    });
    try {
      final dio = ref.read(apiClientProvider);
      final resp = await dio.get('/api/library/${widget.mediaType}', queryParameters: {'view': view});
      if (!mounted) return;
      final data = Map<String, dynamic>.from(resp.data as Map);

      // Client-side fallback if connected to an older backend without ?view= support
      if (data.containsKey('items') &&
          !data.containsKey('categories') &&
          !data.containsKey('collections') &&
          !data.containsKey('recommended')) {
        final rawItems = (data['items'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
        if (view == 'recommended') {
          final byRating = List<Map<String, dynamic>>.from(rawItems)
            ..sort((a, b) => ((b['rating'] as num?) ?? 0).compareTo((a['rating'] as num?) ?? 0));
          final byNewest = List<Map<String, dynamic>>.from(rawItems)
            ..sort((a, b) => ((b['id'] as num?) ?? 0).compareTo((a['id'] as num?) ?? 0));
          setState(() {
            _auxData = {
              'recommended': rawItems.where((i) => i['is_watched'] != true).take(20).toList(),
              'top_rated': byRating.take(20).toList(),
              'recently_added': byNewest.take(20).toList(),
            };
            _auxLoading = false;
          });
          return;
        }
      }

      setState(() {
        _auxData = data;
        _auxLoading = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _auxError = formatApiError(e, fallback: 'Unable to load $view view.');
          _auxLoading = false;
        });
      }
    }
  }

  List<(String, String)> _getSubTabs() {
    switch (widget.mediaType) {
      case 'movie':
      case 'tv':
        return const [
          ('library', 'Library'),
          ('recommended', 'Recommended'),
          ('categories', 'Categories'),
          ('collections', 'Collections'),
        ];
      case 'photo':
        return const [
          ('library', 'All Photos'),
          ('recommended', 'Recommended'),
          ('timeline', 'Timeline'),
        ];
      case 'music':
        return const [
          ('albums', 'Albums'),
          ('artists', 'Artists'),
          ('tracks', 'Tracks'),
          ('playlists', 'Playlists'),
        ];
      default:
        return const [('library', 'Library')];
    }
  }

  List<(String, String)> _getSortOptions() {
    if (widget.mediaType == 'photo') {
      return const [
        ('title_asc', 'Title A–Z'),
        ('title_desc', 'Title Z–A'),
        ('date_desc', 'Newest Added'),
        ('date_asc', 'Oldest Added'),
      ];
    }
    if (widget.mediaType == 'music') {
      return const [
        ('title_asc', 'Title A–Z'),
        ('title_desc', 'Title Z–A'),
        ('year_desc', 'Release Year (Newest)'),
        ('year_asc', 'Release Year (Oldest)'),
      ];
    }
    return const [
      ('title_asc', 'Title A–Z'),
      ('title_desc', 'Title Z–A'),
      ('date_desc', 'Recently Added'),
      ('date_asc', 'Oldest Added'),
      ('year_desc', 'Release Date (Newest)'),
      ('year_asc', 'Release Date (Oldest)'),
      ('rating_desc', 'Rating (Highest)'),
      ('rating_asc', 'Rating (Lowest)'),
    ];
  }

  void _selectSubTab(String tab) {
    if (_subTab == tab) return;
    setState(() {
      _subTab = tab;
      if (tab != 'library') {
        _activeGenreFilter = null;
        _activeCollectionFilter = null;
      }
    });
    if (widget.mediaType == 'music' || tab == 'library') {
      _loadInitial();
    } else if ((widget.mediaType == 'movie' || widget.mediaType == 'tv') &&
        (tab == 'recommended' || tab == 'categories' || tab == 'collections')) {
      _loadAuxView(tab);
    }
  }

  Future<void> _showCreatePlaylistDialog() async {
    final ctrl = TextEditingController();
    final created = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('New Playlist'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: const InputDecoration(labelText: 'Playlist Name'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () async {
              final name = ctrl.text.trim();
              if (name.isEmpty) return;
              await ref.read(apiClientProvider).post('/api/playlists', data: {'name': name});
              if (ctx.mounted) Navigator.pop(ctx, true);
            },
            child: const Text('Create'),
          ),
        ],
      ),
    );
    if (created == true) {
      _loadInitial();
    }
  }

  @override
  Widget build(BuildContext context) {
    final serverUrl = ref.watch(serverUrlProvider) ?? '';
    final subTabs = _getSubTabs();

    return Scaffold(
      appBar: AppBar(
        title: Text(
          widget.title,
          style: const TextStyle(color: AppColors.text, fontSize: 24, fontWeight: FontWeight.bold),
        ),
        actions: [
          if (widget.mediaType == 'music' && _subTab == 'playlists')
            IconButton(
              icon: const Icon(LucideIcons.plus),
              tooltip: 'Create Playlist',
              onPressed: _showCreatePlaylistDialog,
            ),
          PopupMenuButton<String>(
            icon: const Icon(LucideIcons.arrowDownUp),
            color: AppColors.surface,
            onSelected: (value) {
              setState(() => _sort = value);
              _loadInitial();
            },
            itemBuilder: (context) => _getSortOptions()
                .map(
                  (opt) => PopupMenuItem<String>(
                    value: opt.$1,
                    child: Row(
                      children: [
                        if (_sort == opt.$1) const Icon(LucideIcons.check, size: 15, color: AppColors.accent),
                        if (_sort == opt.$1) const SizedBox(width: 6),
                        Text(opt.$2),
                      ],
                    ),
                  ),
                )
                .toList(),
          ),
          IconButton(
            icon: const Icon(LucideIcons.search),
            onPressed: () => context.push('/search'),
          ),
          const SizedBox(width: 4),
          const ProfileAvatarButton(),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(48),
          child: SizedBox(
            height: 48,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
              children: [
                ...subTabs.map((t) {
                  final selected = _subTab == t.$1;
                  return Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(t.$2),
                      selected: selected,
                      selectedColor: AppColors.accent,
                      backgroundColor: AppColors.surfaceHover,
                      labelStyle: TextStyle(
                        color: selected ? AppColors.text : AppColors.textMuted,
                        fontWeight: FontWeight.bold,
                      ),
                      onSelected: (sel) {
                        if (sel) _selectSubTab(t.$1);
                      },
                    ),
                  );
                }),
                if (_activeGenreFilter != null || _activeCollectionFilter != null)
                  InputChip(
                    label: Text(_activeGenreFilter ?? _activeCollectionFilter ?? ''),
                    backgroundColor: AppColors.accentTransparent,
                    deleteIconColor: AppColors.text,
                    onDeleted: () {
                      setState(() {
                        _activeGenreFilter = null;
                        _activeCollectionFilter = null;
                      });
                      _loadInitial();
                    },
                  ),
              ],
            ),
          ),
        ),
      ),
      body: _buildBody(serverUrl),
    );
  }

  Widget _buildBody(String serverUrl) {
    if (widget.mediaType == 'photo' && _subTab == 'recommended') {
      return _buildPhotoRecommendedView(serverUrl);
    }
    if (widget.mediaType == 'photo' && _subTab == 'timeline') {
      return _buildPhotoTimelineView(serverUrl);
    }
    if ((widget.mediaType == 'movie' || widget.mediaType == 'tv') && _subTab != 'library') {
      return _buildMovieTvAuxView(serverUrl);
    }
    return _buildPaginatedGrid(serverUrl);
  }

  Widget _buildPaginatedGrid(String serverUrl) {
    if (_isLoadingInitial) {
      return const Center(child: CircularProgressIndicator(color: AppColors.accent));
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: AppColors.accent)),
              const SizedBox(height: 12),
              ElevatedButton.icon(
                onPressed: _loadInitial,
                icon: const Icon(LucideIcons.refreshCw, size: 15),
                label: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
    }
    if (_items.isEmpty) {
      return const Center(child: Text('No items found', style: TextStyle(color: AppColors.textMuted)));
    }

    final isPhoto = widget.mediaType == 'photo';
    final isMusic = widget.mediaType == 'music';
    final aspect = (isPhoto || isMusic) ? 1.0 : (2 / 3);
    final cols = isMusic ? 2 : 3;

    if (isMusic && _subTab == 'tracks') {
      return RefreshIndicator(
        onRefresh: _loadInitial,
        child: ListView.builder(
          controller: _scrollController,
          padding: const EdgeInsets.all(12),
          itemCount: _items.length + (_isLoadingMore ? 1 : 0),
          itemBuilder: (ctx, idx) {
            if (idx >= _items.length) {
              return const Padding(
                padding: EdgeInsets.all(16),
                child: Center(child: CircularProgressIndicator(color: AppColors.accent)),
              );
            }
            final trk = _items[idx];
            final imgUrl = buildMetadataImageUrl(serverUrl, trk['poster_filename']?.toString());
            return ListTile(
              leading: ClipRRect(
                borderRadius: BorderRadius.circular(AppSpacing.radius),
                child: Container(
                  width: 44,
                  height: 44,
                  color: AppColors.surface,
                  child: imgUrl != null
                      ? CachedNetworkImage(imageUrl: imgUrl, fit: BoxFit.cover)
                      : const Icon(LucideIcons.music, color: AppColors.textMuted),
                ),
              ),
              title: Text(trk['title']?.toString() ?? 'Track', maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: Text(
                '${trk['artist_name'] ?? ''} • ${trk['year'] ?? ''}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: AppColors.textMuted, fontSize: 12),
              ),
              onTap: () => ref.read(audioPlayerProvider.notifier).playQueue(
                    _items.map((m) => AudioTrackItem.fromJson(m)).toList(),
                    startIndex: idx,
                  ),
            );
          },
        ),
      );
    }

    return Column(
      children: [
        if (isPhoto)
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 10, 14, 2),
            child: Row(
              children: [
                if (_currentPhotoAlbum != null)
                  IconButton(
                    icon: const Icon(LucideIcons.arrowLeft, size: 18, color: AppColors.textMuted),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                    onPressed: () {
                      setState(() {
                        _currentPhotoAlbum = _photoAlbumHistory.isNotEmpty ? _photoAlbumHistory.removeLast() : null;
                      });
                      _loadInitial();
                    },
                  ),
                if (_currentPhotoAlbum != null) const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    _currentPhotoAlbum == null ? 'Albums & Photos' : _currentPhotoAlbum!,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: AppColors.text, fontSize: 16, fontWeight: FontWeight.bold),
                  ),
                ),
              ],
            ),
          ),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _loadInitial,
            child: GridView.builder(
              controller: _scrollController,
              padding: const EdgeInsets.all(14),
              gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: cols,
                childAspectRatio: (isPhoto || isMusic) ? 0.72 : 0.54,
                crossAxisSpacing: 10,
                mainAxisSpacing: 12,
              ),
              itemCount: _items.length + (_isLoadingMore ? 1 : 0),
              itemBuilder: (context, index) {
                if (index >= _items.length) {
                  return const Center(child: CircularProgressIndicator(color: AppColors.accent));
                }
                final item = _items[index];
                final posterUrl = buildMetadataImageUrl(serverUrl, item['poster_filename']?.toString());
                final itemType = (item['type'] ?? widget.mediaType).toString();
                final subtitle = itemType == 'album' && isPhoto
                    ? 'Folder'
                    : (item['artist_name']?.toString() ?? item['year']?.toString());

                return MediaCard(
                  itemId: item['id'],
                  title: item['title']?.toString() ?? 'Unknown',
                  subtitle: subtitle,
                  imageUrl: posterUrl,
                  mediaType: itemType,
                  aspectRatio: aspect,
                  expandImage: true,
                  isWatched: item['is_watched'] == true,
                  hasProgress: item['has_progress'] == true,
                  inWatchlist: item['in_watchlist'] == true,
                  year: item['year']?.toString(),
                  showActionMenu: itemType == 'movie' || itemType == 'tv',
                  onMutated: _loadInitial,
                  onTap: () {
                    if (isPhoto) {
                      if (itemType == 'album') {
                        setState(() {
                          _photoAlbumHistory.add(_currentPhotoAlbum);
                          _currentPhotoAlbum = item['album']?.toString() ?? item['id']?.toString();
                        });
                        _loadInitial();
                      } else {
                        showHeirloomPhotoViewer(context, item, serverUrl);
                      }
                    } else if (isMusic) {
                      if (itemType == 'artist') {
                        context.push('/artist/${item['id']}');
                      } else if (itemType == 'playlist') {
                        context.push('/playlist/${item['id']}');
                      } else {
                        context.push('/album/${item['id']}');
                      }
                    } else {
                      final type = (widget.mediaType == 'tv' || itemType == 'tv') ? 'tv' : 'movie';
                      context.push('/details/$type/${item['id']}');
                    }
                  },
                );
              },
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildMovieTvAuxView(String serverUrl) {
    if (_auxLoading) {
      return const Center(child: CircularProgressIndicator(color: AppColors.accent));
    }
    if (_auxError != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(_auxError!, textAlign: TextAlign.center, style: const TextStyle(color: AppColors.accent)),
              const SizedBox(height: 12),
              ElevatedButton.icon(
                onPressed: () => _loadAuxView(_subTab),
                icon: const Icon(LucideIcons.refreshCw, size: 15),
                label: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
    }
    if (_auxData == null) {
      return const SizedBox.shrink();
    }

    if (_subTab == 'categories') {
      final cats = (_auxData!['categories'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
      if (cats.isEmpty) {
        return const Center(child: Text('No categories found', style: TextStyle(color: AppColors.textMuted)));
      }
      return GridView.builder(
        padding: const EdgeInsets.all(14),
        gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
          crossAxisCount: 2,
          childAspectRatio: 1.6,
          crossAxisSpacing: 10,
          mainAxisSpacing: 10,
        ),
        itemCount: cats.length,
        itemBuilder: (ctx, i) {
          final c = cats[i];
          final bgUrl = buildMetadataImageUrl(
            serverUrl,
            (c['backdrop_filename'] ?? c['poster_filename'])?.toString(),
          );
          return InkWell(
            borderRadius: BorderRadius.circular(AppSpacing.radius),
            onTap: () {
              setState(() {
                _activeGenreFilter = c['name']?.toString();
                _subTab = 'library';
              });
              _loadInitial();
            },
            child: ClipRRect(
              borderRadius: BorderRadius.circular(AppSpacing.radius),
              child: Stack(
                fit: StackFit.expand,
                children: [
                  if (bgUrl != null)
                    CachedNetworkImage(imageUrl: bgUrl, fit: BoxFit.cover)
                  else
                    Container(color: AppColors.surface),
                  Container(color: AppColors.overlay),
                  Center(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          c['name']?.toString() ?? '',
                          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: AppColors.text),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          '${c['count'] ?? 0} titles',
                          style: const TextStyle(fontSize: 12, color: AppColors.textMuted),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      );
    }

    if (_subTab == 'collections') {
      final cols = (_auxData!['collections'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
      if (cols.isEmpty) {
        return const Center(child: Text('No collections found', style: TextStyle(color: AppColors.textMuted)));
      }
      return GridView.builder(
        padding: const EdgeInsets.all(14),
        gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
          crossAxisCount: 2,
          childAspectRatio: 0.62,
          crossAxisSpacing: 12,
          mainAxisSpacing: 12,
        ),
        itemCount: cols.length,
        itemBuilder: (ctx, i) {
          final col = cols[i];
          final posterUrl = buildMetadataImageUrl(serverUrl, col['poster_filename']?.toString());
          return MediaCard(
            itemId: col['name'],
            title: col['name']?.toString() ?? 'Collection',
            subtitle: '${col['count'] ?? 0} items${col['year_range'] != null && col['year_range'].toString().isNotEmpty ? ' • ${col['year_range']}' : ''}',
            imageUrl: posterUrl,
            mediaType: 'collection',
            expandImage: true,
            showActionMenu: false,
            onTap: () {
              setState(() {
                _activeCollectionFilter = col['name']?.toString();
                _subTab = 'library';
              });
              _loadInitial();
            },
          );
        },
      );
    }

    final sections = <(String, List<dynamic>)>[
      ('Recommended For You', _auxData!['recommended'] as List? ?? []),
      ('Top Rated', _auxData!['top_rated'] as List? ?? []),
      ('Recently Added', _auxData!['recently_added'] as List? ?? []),
      ('Recently Released', _auxData!['recently_released'] as List? ?? []),
    ];

    if (sections.every((s) => s.$2.isEmpty)) {
      return const Center(child: Text('No recommendations available yet', style: TextStyle(color: AppColors.textMuted)));
    }

    return ListView(
      padding: const EdgeInsets.symmetric(vertical: 14),
      children: sections.where((s) => s.$2.isNotEmpty).map((s) {
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: Text(s.$1, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: AppColors.text)),
            ),
            SizedBox(
              height: 245,
              child: ListView.builder(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                itemCount: s.$2.length,
                itemBuilder: (ctx, i) {
                  final item = Map<String, dynamic>.from(s.$2[i] as Map);
                  final url = buildMetadataImageUrl(serverUrl, item['poster_filename']?.toString());
                  return Container(
                    width: 134,
                    margin: const EdgeInsets.symmetric(horizontal: 4),
                    child: MediaCard(
                      itemId: item['id'],
                      title: item['title']?.toString() ?? '',
                      subtitle: item['year']?.toString(),
                      imageUrl: url,
                      mediaType: widget.mediaType,
                      isWatched: item['is_watched'] == true,
                      onMutated: () => _loadAuxView('recommended'),
                      onTap: () => context.push('/details/${widget.mediaType}/${item['id']}'),
                    ),
                  );
                },
              ),
            ),
          ],
        );
      }).toList(),
    );
  }

  Widget _buildPhotoRecommendedView(String serverUrl) {
    final recommendedAsync = ref.watch(photoRecommendedProvider);
    return recommendedAsync.when(
      loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
      error: (err, stack) => Center(child: Text(formatApiError(err), style: const TextStyle(color: AppColors.accent))),
      data: (sections) {
        if (sections.isEmpty) {
          return const Center(child: Text('No recommendations available', style: TextStyle(color: AppColors.textMuted)));
        }
        return RefreshIndicator(
          onRefresh: () => ref.refresh(photoRecommendedProvider.future),
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(vertical: 14),
            itemCount: sections.length,
            itemBuilder: (context, secIndex) {
              final section = sections[secIndex];
              final title = section['title'] as String? ?? 'Recommended';
              final photos = (section['photos'] as List? ?? []).map((p) => Map<String, dynamic>.from(p as Map)).toList();
              if (photos.isEmpty) return const SizedBox.shrink();
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16.0, vertical: 8.0),
                    child: Text(title, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: AppColors.text)),
                  ),
                  SizedBox(
                    height: 185,
                    child: ListView.builder(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      itemCount: photos.length,
                      itemBuilder: (context, pIndex) {
                        final photo = photos[pIndex];
                        final imgUrl = buildMetadataImageUrl(serverUrl, photo['poster_filename']?.toString());
                        return Container(
                          width: 145,
                          margin: const EdgeInsets.symmetric(horizontal: 4),
                          child: MediaCard(
                            itemId: photo['id'],
                            title: photo['title']?.toString() ?? 'Photo',
                            subtitle: photo['date_display']?.toString(),
                            imageUrl: imgUrl,
                            mediaType: 'photo',
                            aspectRatio: 1.0,
                            showActionMenu: false,
                            onTap: () => showHeirloomPhotoViewer(context, photo, serverUrl),
                          ),
                        );
                      },
                    ),
                  ),
                  const SizedBox(height: 12),
                ],
              );
            },
          ),
        );
      },
    );
  }

  Widget _buildPhotoTimelineView(String serverUrl) {
    final timelineAsync = ref.watch(photoTimelineProvider);
    return timelineAsync.when(
      loading: () => const Center(child: CircularProgressIndicator(color: AppColors.accent)),
      error: (err, _) => Center(child: Text(formatApiError(err), style: const TextStyle(color: AppColors.accent))),
      data: (items) {
        if (items.isEmpty) {
          return const Center(child: Text('No timeline photos found', style: TextStyle(color: AppColors.textMuted)));
        }
        final grouped = <String, List<Map<String, dynamic>>>{};
        for (final p in items) {
          final dayTitle = (p['day_title'] ?? 'Undated').toString();
          grouped.putIfAbsent(dayTitle, () => []).add(p);
        }

        final entries = grouped.entries.toList();
        return RefreshIndicator(
          onRefresh: () => ref.refresh(photoTimelineProvider.future),
          child: ListView.builder(
            padding: const EdgeInsets.all(14),
            itemCount: entries.length,
            itemBuilder: (ctx, idx) {
              final entry = entries[idx];
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Text(entry.key, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: AppColors.text)),
                  ),
                  GridView.builder(
                    shrinkWrap: true,
                    physics: const NeverScrollableScrollPhysics(),
                    gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                      crossAxisCount: 3,
                      childAspectRatio: 0.78,
                      crossAxisSpacing: 8,
                      mainAxisSpacing: 8,
                    ),
                    itemCount: entry.value.length,
                    itemBuilder: (c, pIdx) {
                      final p = entry.value[pIdx];
                      final url = buildMetadataImageUrl(serverUrl, p['poster_filename']?.toString());
                      return MediaCard(
                        itemId: p['id'],
                        title: p['title']?.toString() ?? 'Photo',
                        imageUrl: url,
                        mediaType: 'photo',
                        expandImage: true,
                        showActionMenu: false,
                        onTap: () => showHeirloomPhotoViewer(context, p, serverUrl),
                      );
                    },
                  ),
                  const SizedBox(height: 12),
                ],
              );
            },
          ),
        );
      },
    );
  }
}
