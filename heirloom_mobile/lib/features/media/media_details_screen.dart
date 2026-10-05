import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:cached_network_image/cached_network_image.dart';
import 'package:go_router/go_router.dart';
import 'package:dio/dio.dart' as dio_pkg;
import '../../core/api_client.dart';
import '../../core/theme.dart';

final mediaDetailsProvider = FutureProvider.family<Map<String, dynamic>, String>((ref, param) async {
  final dio = ref.watch(apiClientProvider);
  final parts = param.split(':');
  final mediaType = parts[0];
  final id = parts[1];
  
  // Uses the newly created /api/ JSON endpoints
  final endpoint = mediaType == 'movie' ? '/api/movie/$id' : '/api/series/$id';
  
  final response = await dio.get(
    endpoint,
    options: dio_pkg.Options(headers: {'Accept': 'application/json'}),
  );
  return response.data as Map<String, dynamic>;
});

class MediaDetailsScreen extends ConsumerWidget {
  final String mediaType; // "movie" or "tv"
  final String id;

  const MediaDetailsScreen({
    super.key,
    required this.mediaType,
    required this.id,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detailsAsync = ref.watch(mediaDetailsProvider('$mediaType:$id'));
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    return Scaffold(
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft, color: Colors.white, shadows: [Shadow(color: Colors.black, blurRadius: 4)]),
          onPressed: () => context.pop(),
        ),
      ),
      body: detailsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (err, stack) => Center(child: Text('Error loading details: $err', style: const TextStyle(color: Colors.red))),
        data: (data) {
          // Both Movie and TV show objects are typically passed inside an "item" or similar root key
          // We'll adapt based on what the JSON returns
          final item = data['item'] ?? data['movie'] ?? data['tv_show'] ?? data;
          
          final backdrop = item['backdrop_filename'];
          final poster = item['poster_filename'];
          final title = item['title'] ?? 'Unknown Title';
          final plot = item['plot'] ?? 'No plot available.';
          final year = item['year'] ?? '';
          final rating = item['rating']?.toString() ?? '';
          final runtime = item['runtime']?.toString() ?? '';
          final contentRating = item['content_rating'] ?? '';

          final backdropUrl = backdrop != null ? '$serverUrl/metadata/$backdrop' : null;
          final posterUrl = poster != null ? '$serverUrl/metadata/$poster' : null;

          return SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Backdrop image
                SizedBox(
                  height: 300,
                  width: double.infinity,
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      if (backdropUrl != null)
                        CachedNetworkImage(
                          imageUrl: backdropUrl,
                          fit: BoxFit.cover,
                        )
                      else
                        Container(color: AppTheme.bgTertiary),
                      // Gradient overlay
                      Container(
                        decoration: BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topCenter,
                            end: Alignment.bottomCenter,
                            colors: [
                              Colors.transparent,
                              AppTheme.bgPrimary.withOpacity(0.8),
                              AppTheme.bgPrimary,
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                
                // Content
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16.0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold),
                      ),
                      const SizedBox(height: 8),
                      Row(
                        children: [
                          if (year.isNotEmpty) _buildTag(year),
                          if (contentRating.isNotEmpty) _buildTag(contentRating),
                          if (runtime.isNotEmpty) _buildTag('$runtime min'),
                          if (rating.isNotEmpty) ...[
                            const Icon(LucideIcons.star, color: Colors.yellow, size: 16),
                            const SizedBox(width: 4),
                            Text(rating, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
                          ],
                        ],
                      ),
                      const SizedBox(height: 24),
                      
                      // Play Button
                      SizedBox(
                        width: double.infinity,
                        child: ElevatedButton.icon(
                          onPressed: () {
                            context.push('/play/$mediaType/$id');
                          },
                          icon: const Icon(LucideIcons.play),
                          label: const Text('Play', style: TextStyle(fontSize: 18)),
                        ),
                      ),
                      const SizedBox(height: 16),
                      
                      // Action Buttons
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                        children: [
                          _buildActionButton(LucideIcons.plus, 'Watchlist', () {}),
                          _buildActionButton(LucideIcons.download, 'Download', () {}),
                          _buildActionButton(LucideIcons.users, 'Watch Together', () {}),
                        ],
                      ),
                      
                      const SizedBox(height: 24),
                      Text(
                        plot,
                        style: const TextStyle(fontSize: 16, color: AppTheme.textSecondary, height: 1.5),
                      ),
                      const SizedBox(height: 40),
                    ],
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildTag(String text) {
    return Container(
      margin: const EdgeInsets.only(right: 8),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: AppTheme.bgTertiary,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(text, style: const TextStyle(fontSize: 12, color: AppTheme.textSecondary)),
    );
  }

  Widget _buildActionButton(IconData icon, String label, VoidCallback onTap) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.all(8.0),
        child: Column(
          children: [
            Icon(icon, color: Colors.white, size: 28),
            const SizedBox(height: 4),
            Text(label, style: const TextStyle(fontSize: 12, color: AppTheme.textSecondary)),
          ],
        ),
      ),
    );
  }
}
