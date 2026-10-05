import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

final dashboardDataProvider = FutureProvider<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/dashboard');
  return response.data as Map<String, dynamic>;
});

class DashboardHome extends ConsumerWidget {
  const DashboardHome({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final dashboardData = ref.watch(dashboardDataProvider);
    final serverUrl = ref.watch(serverUrlProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Heirloom', style: TextStyle(color: AppTheme.primaryRed, fontSize: 24)),
        centerTitle: false,
        actions: [
          IconButton(
            icon: const Icon(LucideIcons.search),
            onPressed: () {
              context.push('/search');
            },
          ),
          IconButton(
            icon: const Icon(LucideIcons.inbox),
            onPressed: () {
              // TODO: Implement inbox
            },
          ),
          IconButton(
            icon: const Icon(LucideIcons.settings),
            onPressed: () {
              context.push('/settings');
            },
          ),
        ],
      ),
      body: dashboardData.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
        data: (data) {
          final history = data['history'] as List<dynamic>? ?? [];
          final recentMovies = data['recent_movies'] as List<dynamic>? ?? [];
          final recentTv = data['recent_tv'] as List<dynamic>? ?? [];
          final watchlist = data['watchlist'] as List<dynamic>? ?? [];
          final recommended = data['recommended'] as List<dynamic>? ?? [];
          final recentPhotos = data['recent_photos'] as List<dynamic>? ?? [];

          return RefreshIndicator(
            onRefresh: () => ref.refresh(dashboardDataProvider.future),
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 16),
              children: [
                if (history.isNotEmpty)
                  _buildCarousel('Continue Watching', history, serverUrl ?? '', isHistory: true),
                if (watchlist.isNotEmpty)
                  _buildCarousel('My Watchlist', watchlist, serverUrl ?? '', isHistory: true),
                if (recommended.isNotEmpty)
                  _buildCarousel('Recommended For You', recommended, serverUrl ?? '', isHistory: true),
                if (recentMovies.isNotEmpty)
                  _buildCarousel('Recently Added Movies', recentMovies, serverUrl ?? '', defaultMediaType: 'movie'),
                if (recentTv.isNotEmpty)
                  _buildCarousel('Recently Added TV Shows', recentTv, serverUrl ?? '', defaultMediaType: 'tv'),
                if (recentPhotos.isNotEmpty)
                  _buildCarousel('Recent Photos', recentPhotos, serverUrl ?? '', defaultMediaType: 'photo'),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildCarousel(String title, List<dynamic> items, String serverUrl, {bool isHistory = false, String? defaultMediaType}) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16.0, vertical: 8.0),
          child: Text(
            title,
            style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
          ),
        ),
        SizedBox(
          height: 220,
          child: ListView.builder(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12),
            itemCount: items.length,
            itemBuilder: (context, index) {
              final rawItem = items[index];
              final item = isHistory ? rawItem['item'] : rawItem;
              final progress = (isHistory && rawItem['progress'] != null) ? (rawItem['progress'] as num).toDouble() : null;
              final mediaType = isHistory ? rawItem['media_type'] : defaultMediaType;

              return _buildMediaCard(context, item, serverUrl, progress, mediaType);
            },
          ),
        ),
        const SizedBox(height: 16),
      ],
    );
  }

  Widget _buildMediaCard(BuildContext context, dynamic item, String serverUrl, double? progress, String? mediaType) {
    final posterFilename = item['poster_filename'];
    final imageUrl = posterFilename != null ? '$serverUrl/metadata/$posterFilename' : null;
    final title = item['title'] ?? 'Unknown';

    return GestureDetector(
      onTap: () {
        if (mediaType != null) {
          context.push('/details/$mediaType/${item['id']}');
        }
      },
      child: Container(
        width: 140,
        margin: const EdgeInsets.symmetric(horizontal: 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Stack(
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: Container(
                      color: AppTheme.bgTertiary,
                      width: double.infinity,
                      height: double.infinity,
                      child: imageUrl != null
                          ? CachedNetworkImage(
                              imageUrl: imageUrl,
                              fit: BoxFit.cover,
                              errorWidget: (context, url, error) => const Icon(LucideIcons.imageOff, size: 40),
                            )
                          : const Center(child: Icon(LucideIcons.imageOff, size: 40)),
                    ),
                  ),
                  if (progress != null && progress > 0)
                    Positioned(
                      bottom: 0,
                      left: 0,
                      right: 0,
                      child: Container(
                        height: 4,
                        color: Colors.white.withOpacity(0.3),
                        child: FractionallySizedBox(
                          alignment: Alignment.centerLeft,
                          widthFactor: progress / 100,
                          child: Container(color: AppTheme.primaryRed),
                        ),
                      ),
                    ),
                ],
              ),
            ),
            const SizedBox(height: 8),
            Text(
              title,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 14),
            ),
          ],
        ),
      ),
    );
  }
}
