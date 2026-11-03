import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets/profile_avatar_button.dart';
import '../media/photo_viewer_dialog.dart';

final libraryProvider = FutureProvider.family<List<dynamic>, String>((ref, params) async {
  final dio = ref.watch(apiClientProvider);
  final parts = params.split(':');
  final mediaType = parts[0];
  final sort = parts[1];
  
  final response = await dio.get('/api/library/$mediaType', queryParameters: {
    'limit': 100,
    'sort': sort,
  });
  return response.data['items'] as List<dynamic>;
});

final photoRecommendedProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/photos/recommended');
  final sections = response.data['sections'] as List<dynamic>? ?? [];
  return sections.map((s) => Map<String, dynamic>.from(s as Map)).toList();
});

final librarySortProvider = StateProvider.family<String, String>((ref, mediaType) => 'title_asc');

class LibraryScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String title;

  const LibraryScreen({super.key, required this.mediaType, required this.title});

  @override
  ConsumerState<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends ConsumerState<LibraryScreen> {
  int _photoTab = 0; // 0 = All Photos, 1 = Recommended

  @override
  Widget build(BuildContext context) {
    final sort = ref.watch(librarySortProvider(widget.mediaType));
    final libraryAsync = ref.watch(libraryProvider('${widget.mediaType}:$sort'));
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    final isPhoto = widget.mediaType == 'photo';
    final isMusic = widget.mediaType == 'music';

    return Scaffold(
      appBar: AppBar(
        title: Text(widget.title, style: const TextStyle(color: AppTheme.primaryRed, fontSize: 24, fontWeight: FontWeight.bold)),
        centerTitle: false,
        actions: [
          PopupMenuButton<String>(
            icon: const Icon(LucideIcons.arrowDownUp),
            onSelected: (value) => ref.read(librarySortProvider(widget.mediaType).notifier).state = value,
            itemBuilder: (context) => [
              const PopupMenuItem(value: 'title_asc', child: Text('A-Z')),
              const PopupMenuItem(value: 'title_desc', child: Text('Z-A')),
              const PopupMenuItem(value: 'date_desc', child: Text('Newest')),
              const PopupMenuItem(value: 'date_asc', child: Text('Oldest')),
            ],
          ),
          IconButton(
            icon: const Icon(LucideIcons.search),
            onPressed: () => context.push('/search'),
          ),
          const SizedBox(width: 4),
          const ProfileAvatarButton(),
        ],
        bottom: isPhoto
            ? PreferredSize(
                preferredSize: const Size.fromHeight(48),
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16.0, vertical: 6.0),
                  child: Row(
                    children: [
                      ChoiceChip(
                        label: const Text('All Photos'),
                        selected: _photoTab == 0,
                        selectedColor: AppTheme.primaryRed,
                        backgroundColor: AppTheme.bgTertiary,
                        labelStyle: TextStyle(
                          color: _photoTab == 0 ? Colors.white : AppTheme.textTertiary,
                          fontWeight: _photoTab == 0 ? FontWeight.bold : FontWeight.normal,
                        ),
                        onSelected: (sel) {
                          if (sel) setState(() => _photoTab = 0);
                        },
                      ),
                      const SizedBox(width: 8),
                      ChoiceChip(
                        label: const Text('Recommended'),
                        selected: _photoTab == 1,
                        selectedColor: AppTheme.primaryRed,
                        backgroundColor: AppTheme.bgTertiary,
                        labelStyle: TextStyle(
                          color: _photoTab == 1 ? Colors.white : AppTheme.textTertiary,
                          fontWeight: _photoTab == 1 ? FontWeight.bold : FontWeight.normal,
                        ),
                        onSelected: (sel) {
                          if (sel) setState(() => _photoTab = 1);
                        },
                      ),
                    ],
                  ),
                ),
              )
            : null,
      ),
      body: isPhoto && _photoTab == 1
          ? _buildPhotoRecommendedView(serverUrl)
          : _buildStandardLibraryGrid(libraryAsync, serverUrl, isMusic),
    );
  }

  Widget _buildStandardLibraryGrid(AsyncValue<List<dynamic>> libraryAsync, String serverUrl, bool isMusic) {
    return libraryAsync.when(
      loading: () => const Center(child: CircularProgressIndicator(color: AppTheme.primaryRed)),
      error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
      data: (items) {
        if (items.isEmpty) {
          return const Center(child: Text('No items found', style: TextStyle(color: AppTheme.textSecondary)));
        }

        return RefreshIndicator(
          onRefresh: () => ref.refresh(libraryProvider('${widget.mediaType}:${ref.read(librarySortProvider(widget.mediaType))}').future),
          child: GridView.builder(
            padding: const EdgeInsets.all(16),
            gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: widget.mediaType == 'photo' ? 3 : (isMusic ? 2 : 3),
              childAspectRatio: widget.mediaType == 'photo' ? 1.0 : (isMusic ? 0.82 : 0.65),
              crossAxisSpacing: 12,
              mainAxisSpacing: 12,
            ),
            itemCount: items.length,
            itemBuilder: (context, index) {
              final item = items[index];
              final posterFilename = item['poster_filename'];
              final imageUrl = posterFilename != null ? '$serverUrl/metadata/$posterFilename' : null;

              return GestureDetector(
                onTap: () {
                  if (widget.mediaType == 'photo' || item['type'] == 'photo' || item['type'] == 'album') {
                    showHeirloomPhotoViewer(context, Map<String, dynamic>.from(item), serverUrl);
                  } else if (widget.mediaType == 'music') {
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(content: Text('Album: ${item['title']} (${item['artist_name'] ?? ''})')),
                    );
                  } else {
                    final type = (widget.mediaType == 'tv' || item['type'] == 'tv' || item['type'] == 'tvshow') ? 'tv' : 'movie';
                    context.push('/details/$type/${item['id']}');
                  }
                },
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: Container(
                          color: AppTheme.bgTertiary,
                          width: double.infinity,
                          child: imageUrl != null
                              ? CachedNetworkImage(
                                  imageUrl: imageUrl,
                                  fit: BoxFit.cover,
                                  errorWidget: (context, url, error) => const Icon(LucideIcons.imageOff, color: AppTheme.textTertiary),
                                )
                              : const Icon(LucideIcons.imageOff, color: AppTheme.textTertiary),
                        ),
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      item['title'] ?? '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
                    ),
                    if (isMusic && item['artist_name'] != null)
                      Text(
                        item['artist_name'],
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 10, color: AppTheme.textTertiary),
                      ),
                  ],
                ),
              );
            },
          ),
        );
      },
    );
  }

  Widget _buildPhotoRecommendedView(String serverUrl) {
    final recommendedAsync = ref.watch(photoRecommendedProvider);

    return recommendedAsync.when(
      loading: () => const Center(child: CircularProgressIndicator(color: AppTheme.primaryRed)),
      error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
      data: (sections) {
        if (sections.isEmpty) {
          return const Center(child: Text('No recommendations available', style: TextStyle(color: AppTheme.textSecondary)));
        }

        return RefreshIndicator(
          onRefresh: () => ref.refresh(photoRecommendedProvider.future),
          child: ListView.builder(
            padding: const EdgeInsets.symmetric(vertical: 16),
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
                    child: Text(
                      title,
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.white),
                    ),
                  ),
                  SizedBox(
                    height: 150,
                    child: ListView.builder(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      itemCount: photos.length,
                      itemBuilder: (context, pIndex) {
                        final photo = photos[pIndex];
                        final posterFilename = photo['poster_filename'];
                        final imgUrl = posterFilename != null ? '$serverUrl/metadata/$posterFilename' : null;

                        return GestureDetector(
                          onTap: () => showHeirloomPhotoViewer(context, photo, serverUrl),
                          child: Container(
                            width: 150,
                            margin: const EdgeInsets.symmetric(horizontal: 4),
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(8),
                              child: imgUrl != null
                                  ? CachedNetworkImage(
                                      imageUrl: imgUrl,
                                      fit: BoxFit.cover,
                                      errorWidget: (_, __, ___) => Container(color: AppTheme.bgTertiary),
                                    )
                                  : Container(color: AppTheme.bgTertiary),
                            ),
                          ),
                        );
                      },
                    ),
                  ),
                  const SizedBox(height: 16),
                ],
              );
            },
          ),
        );
      },
    );
  }
}
