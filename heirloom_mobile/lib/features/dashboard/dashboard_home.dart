import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../core/widgets/media_card.dart';
import '../../core/widgets/profile_avatar_button.dart';
import '../media/photo_viewer_dialog.dart';
import '../social/inbox_screen.dart';

final dashboardDataProvider = FutureProvider<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/dashboard');
  return Map<String, dynamic>.from(response.data as Map);
});

class DashboardHome extends ConsumerWidget {
  const DashboardHome({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final dashboardData = ref.watch(dashboardDataProvider);
    final inboxAsync = ref.watch(inboxDataProvider);
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    final unreadCount = inboxAsync.maybeWhen(
      data: (d) {
        final invites = (d['invites'] as List? ?? []).length;
        final resets = (d['reset_requests'] as List? ?? []).length;
        final downloads = (d['loom_downloads'] as List? ?? []).length;
        return invites + resets + downloads;
      },
      orElse: () => 0,
    );

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'Heirloom',
          style: TextStyle(
              color: AppColors.text,
              fontSize: 24,
              fontWeight: FontWeight.bold),
        ),
        actions: [
          Stack(
            alignment: Alignment.center,
            children: [
              IconButton(
                icon: const Icon(LucideIcons.bell),
                color: AppColors.textMuted,
                tooltip: 'Inbox',
                onPressed: () => context.push('/inbox'),
              ),
              if (unreadCount > 0)
                Positioned(
                  top: 8,
                  right: 8,
                  child: Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                    decoration: BoxDecoration(
                      color: AppColors.accent,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Text(
                      unreadCount > 9 ? '9+' : '$unreadCount',
                      style: const TextStyle(
                          fontSize: 10,
                          fontWeight: FontWeight.bold,
                          color: AppColors.text),
                    ),
                  ),
                ),
            ],
          ),
          IconButton(
            icon: const Icon(LucideIcons.search),
            onPressed: () => context.push('/search'),
          ),
          IconButton(
            icon: const Icon(LucideIcons.settings),
            onPressed: () => context.push('/settings'),
          ),
          const SizedBox(width: 4),
          const ProfileAvatarButton(),
        ],
      ),
      body: dashboardData.when(
        loading: () => const Center(
            child: CircularProgressIndicator(color: AppColors.accent)),
        error: (err, stack) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(formatApiError(err, fallback: 'Unable to load dashboard.'),
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: AppColors.failure)),
                const SizedBox(height: 12),
                ElevatedButton.icon(
                  onPressed: () => ref.invalidate(dashboardDataProvider),
                  icon: const Icon(LucideIcons.refreshCw, size: 16),
                  label: const Text('Retry'),
                ),
              ],
            ),
          ),
        ),
        data: (data) {
          final history = data['history'] as List<dynamic>? ?? [];
          final recentMovies = data['recent_movies'] as List<dynamic>? ?? [];
          final recentTv = data['recent_tv'] as List<dynamic>? ?? [];
          final watchlist = data['watchlist'] as List<dynamic>? ?? [];
          final recommended = data['recommended'] as List<dynamic>? ?? [];
          final recentAlbums = data['recent_albums'] as List<dynamic>? ?? [];
          final recentPhotos = data['recent_photos'] as List<dynamic>? ?? [];

          final watchedMovieIds = Set<int>.from(
            (data['watched_movie_ids'] as List? ?? [])
                .map((e) => int.tryParse('$e') ?? -1),
          );
          final watchedTvTitles = Set<String>.from(
            (data['watched_tv_titles'] as List? ?? [])
                .map((e) => e.toString().toLowerCase()),
          );
          final inProgressMovieIds = Set<int>.from(
            (data['in_progress_movie_ids'] as List? ?? [])
                .map((e) => int.tryParse('$e') ?? -1),
          );
          final inProgressTvTitles = Set<String>.from(
            (data['in_progress_tv_titles'] as List? ?? [])
                .map((e) => e.toString().toLowerCase()),
          );
          final watchlistKeys = <String>{
            for (final w in watchlist)
              if (w is Map && w['item'] is Map)
                '${w['media_type']}:${w['item']['id']}'
          };

          void refresh() => ref.invalidate(dashboardDataProvider);

          if (history.isEmpty &&
              watchlist.isEmpty &&
              recommended.isEmpty &&
              recentMovies.isEmpty &&
              recentTv.isEmpty &&
              recentAlbums.isEmpty &&
              recentPhotos.isEmpty) {
            return RefreshIndicator(
              onRefresh: () => ref.refresh(dashboardDataProvider.future),
              child: ListView(
                children: const [
                  SizedBox(height: 140),
                  Center(
                    child: Text(
                      'No media yet. Add a library volume or scan in Settings.',
                      style: TextStyle(color: AppColors.textMuted),
                    ),
                  ),
                ],
              ),
            );
          }

          return RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(inboxDataProvider);
              final _ = await ref.refresh(dashboardDataProvider.future);
            },
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 14),
              children: [
                if (history.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Continue Watching',
                    items: history,
                    serverUrl: serverUrl,
                    isWrapped: true,
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                    historyScope: 'episode',
                  ),
                if (watchlist.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'My Watchlist',
                    items: watchlist,
                    serverUrl: serverUrl,
                    isWrapped: true,
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
                if (recommended.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Recommended For You',
                    items: recommended,
                    serverUrl: serverUrl,
                    isWrapped: true,
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
                if (recentMovies.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Recently Added Movies',
                    items: recentMovies,
                    serverUrl: serverUrl,
                    defaultMediaType: 'movie',
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
                if (recentTv.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Recently Added TV Shows',
                    items: recentTv,
                    serverUrl: serverUrl,
                    defaultMediaType: 'tv',
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
                if (recentAlbums.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Recently Added Albums',
                    items: recentAlbums,
                    serverUrl: serverUrl,
                    defaultMediaType: 'album',
                    aspectRatio: 1.0,
                    cardWidth: 145,
                    carouselHeight: 200,
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
                if (recentPhotos.isNotEmpty)
                  _buildCarousel(
                    context: context,
                    title: 'Recent Photos',
                    items: recentPhotos,
                    serverUrl: serverUrl,
                    defaultMediaType: 'photo',
                    aspectRatio: 1.0,
                    cardWidth: 145,
                    carouselHeight: 200,
                    watchedMovieIds: watchedMovieIds,
                    watchedTvTitles: watchedTvTitles,
                    inProgressMovieIds: inProgressMovieIds,
                    inProgressTvTitles: inProgressTvTitles,
                    watchlistKeys: watchlistKeys,
                    onMutated: refresh,
                  ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildCarousel({
    required BuildContext context,
    required String title,
    required List<dynamic> items,
    required String serverUrl,
    bool isWrapped = false,
    String? defaultMediaType,
    double aspectRatio = 2 / 3,
    double cardWidth = 136,
    double carouselHeight = 250,
    required Set<int> watchedMovieIds,
    required Set<String> watchedTvTitles,
    required Set<int> inProgressMovieIds,
    required Set<String> inProgressTvTitles,
    required Set<String> watchlistKeys,
    required VoidCallback onMutated,
    String? historyScope,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16.0, vertical: 8.0),
          child: Text(
            title,
            style: const TextStyle(
                fontSize: 19,
                fontWeight: FontWeight.bold,
                color: AppColors.text),
          ),
        ),
        SizedBox(
          height: carouselHeight,
          child: ListView.builder(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12),
            itemCount: items.length,
            itemBuilder: (context, index) {
              final raw = items[index] as Map;
              final item = isWrapped && raw['item'] is Map
                  ? Map<String, dynamic>.from(raw['item'] as Map)
                  : Map<String, dynamic>.from(raw);
              final mediaType =
                  (isWrapped ? raw['media_type'] : null)?.toString() ??
                      defaultMediaType ??
                      'movie';
              final progress = isWrapped && raw['progress'] != null
                  ? (raw['progress'] as num).toDouble()
                  : null;
              final id = item['id'];
              final numericId = int.tryParse('$id') ?? -1;
              final itemTitle = item['title']?.toString() ?? 'Unknown';
              final titleKey = itemTitle.trim().toLowerCase();

              final isWatched = mediaType == 'movie'
                  ? watchedMovieIds.contains(numericId)
                  : (mediaType == 'tv' &&
                      historyScope != 'episode' &&
                      watchedTvTitles.contains(titleKey));
              final hasProg = (progress != null && progress > 0) ||
                  (mediaType == 'movie'
                      ? inProgressMovieIds.contains(numericId)
                      : (mediaType == 'tv' &&
                          inProgressTvTitles.contains(titleKey)));
              final inWatchlist = watchlistKeys.contains('$mediaType:$id');

              final imageUrl = buildMetadataImageUrl(
                  serverUrl, item['poster_filename']?.toString());
              final subtitle = item['artist_name']?.toString() ??
                  item['year']?.toString() ??
                  item['date']?.toString();

              return Container(
                width: cardWidth,
                margin: const EdgeInsets.symmetric(horizontal: 4),
                child: MediaCard(
                  itemId: id,
                  title: itemTitle,
                  subtitle: subtitle,
                  imageUrl: imageUrl,
                  mediaType: mediaType,
                  aspectRatio: aspectRatio,
                  progress: progress,
                  isWatched: isWatched,
                  hasProgress: hasProg,
                  inWatchlist: inWatchlist,
                  year: item['year']?.toString(),
                  historyScope: historyScope,
                  showActionMenu: mediaType == 'movie' || mediaType == 'tv',
                  onMutated: onMutated,
                  onTap: () {
                    if (mediaType == 'photo') {
                      showHeirloomPhotoViewer(context, item, serverUrl);
                    } else if (mediaType == 'album') {
                      context.push('/album/$id');
                    } else if (historyScope == 'episode' && mediaType == 'tv') {
                      context.push('/play/tv/$id');
                    } else {
                      context.push('/details/$mediaType/$id');
                    }
                  },
                ),
              );
            },
          ),
        ),
        const SizedBox(height: 12),
      ],
    );
  }
}
