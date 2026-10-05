import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

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

final librarySortProvider = StateProvider.family<String, String>((ref, mediaType) => 'title_asc');

class LibraryScreen extends ConsumerWidget {
  final String mediaType;
  final String title;

  const LibraryScreen({super.key, required this.mediaType, required this.title});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sort = ref.watch(librarySortProvider(mediaType));
    final libraryAsync = ref.watch(libraryProvider('$mediaType:$sort'));
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    return Scaffold(
      appBar: AppBar(
        title: Text(title, style: const TextStyle(color: AppTheme.primaryRed, fontSize: 24)),
        centerTitle: false,
        actions: [
          PopupMenuButton<String>(
            icon: const Icon(LucideIcons.arrowDownUp),
            onSelected: (value) => ref.read(librarySortProvider(mediaType).notifier).state = value,
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
        ],
      ),
      body: libraryAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (err, stack) => Center(child: Text('Error: $err')),
        data: (items) {
          if (items.isEmpty) {
            return const Center(child: Text('No items found', style: TextStyle(color: AppTheme.textSecondary)));
          }

          return GridView.builder(
            padding: const EdgeInsets.all(16),
            gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
              crossAxisCount: 3,
              childAspectRatio: 0.65,
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
                  if (item['type'] == 'photo' || item['type'] == 'album') {
                    // For photos, the URL might point to a web route, but we don't have a photo viewer yet in flutter
                    // So we can push them to browser or implement a photo viewer later.
                  } else {
                    final type = item['type'] == 'tvshow' ? 'tv' : 'movie';
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
                                  errorWidget: (context, url, error) => const Icon(LucideIcons.imageOff),
                                )
                              : const Icon(LucideIcons.imageOff),
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
                  ],
                ),
              );
            },
          );
        },
      ),
    );
  }
}
