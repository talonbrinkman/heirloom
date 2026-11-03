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
  final colonIdx = param.indexOf(':');
  final mediaType = colonIdx != -1 ? param.substring(0, colonIdx) : 'movie';
  final id = colonIdx != -1 ? param.substring(colonIdx + 1) : param;
  
  if (mediaType == 'photo') {
    final response = await dio.get('/api/photo/$id');
    return response.data as Map<String, dynamic>;
  }

  final endpoint = mediaType == 'movie' ? '/api/movie/$id' : '/api/series/$id';
  try {
    final response = await dio.get(
      endpoint,
      options: dio_pkg.Options(headers: {'Accept': 'application/json'}),
    );
    return response.data as Map<String, dynamic>;
  } on dio_pkg.DioException catch (e) {
    if (e.response?.statusCode == 404) {
      // Fallback: try opposite endpoint in case media type was mismatched
      final fallbackEndpoint = mediaType == 'movie' ? '/api/series/$id' : '/api/movie/$id';
      try {
        final fallbackResp = await dio.get(
          fallbackEndpoint,
          options: dio_pkg.Options(headers: {'Accept': 'application/json'}),
        );
        return fallbackResp.data as Map<String, dynamic>;
      } catch (_) {}
    }
    rethrow;
  }
});

class MediaDetailsScreen extends ConsumerStatefulWidget {
  final String mediaType; // "movie" or "tv"
  final String id;

  const MediaDetailsScreen({
    super.key,
    required this.mediaType,
    required this.id,
  });

  @override
  ConsumerState<MediaDetailsScreen> createState() => _MediaDetailsScreenState();
}

class _MediaDetailsScreenState extends ConsumerState<MediaDetailsScreen> {
  int? _selectedSeason;
  bool? _inWatchlist;

  @override
  Widget build(BuildContext context) {
    final detailsAsync = ref.watch(mediaDetailsProvider('${widget.mediaType}:${widget.id}'));
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
        loading: () => const Center(child: CircularProgressIndicator(color: AppTheme.primaryRed)),
        error: (err, stack) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(LucideIcons.film, size: 48, color: AppTheme.textTertiary),
                const SizedBox(height: 16),
                const Text(
                  'Media Item Not Found',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.white),
                ),
                const SizedBox(height: 8),
                const Text(
                  'This movie or TV show could not be found on the server.',
                  style: TextStyle(color: AppTheme.textTertiary, fontSize: 13),
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 20),
                ElevatedButton.icon(
                  onPressed: () => context.pop(),
                  icon: const Icon(LucideIcons.arrowLeft, size: 16),
                  label: const Text('Go Back'),
                ),
              ],
            ),
          ),
        ),
        data: (data) {
          final item = data['item'] ?? data['movie'] ?? data['tv_show'] ?? data;
          
          final backdrop = item['backdrop_filename'];
          final poster = item['poster_filename'];
          final title = item['title'] ?? 'Unknown Title';
          final plot = item['plot'] ?? 'No plot available.';
          final year = item['year']?.toString() ?? '';
          final rating = item['rating']?.toString() ?? '';
          final runtime = item['runtime']?.toString() ?? '';
          final contentRating = item['content_rating'] ?? '';
          final rawWatchlist = item['in_watchlist'] as bool? ?? false;
          _inWatchlist ??= rawWatchlist;

          final backdropUrl = backdrop != null
              ? '$serverUrl/metadata/$backdrop'
              : (poster != null ? '$serverUrl/metadata/$poster' : null);

          // Parse seasons & episodes if TV Show
          Map<int, List<dynamic>> seasonsMap = {};
          if (widget.mediaType == 'tv' && item['seasons'] != null) {
            final rawSeasons = item['seasons'];
            if (rawSeasons is Map) {
              rawSeasons.forEach((k, v) {
                final sNum = int.tryParse(k.toString()) ?? 1;
                if (v is List) {
                  seasonsMap[sNum] = v;
                }
              });
            }
          }

          final sortedSeasonNumbers = seasonsMap.keys.toList()..sort();
          if (_selectedSeason == null && sortedSeasonNumbers.isNotEmpty) {
            _selectedSeason = sortedSeasonNumbers.first;
          }

          // Determine episode to play on primary "Play" button
          dynamic playEpisodeId = widget.id;
          if (widget.mediaType == 'tv' && seasonsMap.isNotEmpty) {
            final activeEpisodes = seasonsMap[_selectedSeason] ?? [];
            if (activeEpisodes.isNotEmpty) {
              final uncompleted = activeEpisodes.firstWhere(
                (ep) => (ep['progress'] as num? ?? 0) < 95,
                orElse: () => activeEpisodes.first,
              );
              playEpisodeId = uncompleted['id'];
            }
          }

          return SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Backdrop image with gradient
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
                          errorWidget: (_, __, ___) => Container(color: AppTheme.bgTertiary),
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
                
                // Content info
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
                      const SizedBox(height: 20),
                      
                      // Primary Play Button
                      SizedBox(
                        width: double.infinity,
                        child: ElevatedButton.icon(
                          onPressed: () {
                            context.push('/play/${widget.mediaType}/$playEpisodeId');
                          },
                          icon: const Icon(LucideIcons.play),
                          label: Text(
                            widget.mediaType == 'tv' ? 'Play Episode' : 'Play',
                            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                      
                      // Action Buttons (Watchlist toggle, Download, Info)
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                        children: [
                          _buildActionButton(
                            _inWatchlist == true ? LucideIcons.check : LucideIcons.plus,
                            _inWatchlist == true ? 'In Watchlist' : 'Watchlist',
                            () => _toggleWatchlist(item['id']),
                            isActive: _inWatchlist == true,
                          ),
                          _buildActionButton(LucideIcons.download, 'Download', () {
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(content: Text('Offline download coming soon!')),
                            );
                          }),
                          _buildActionButton(LucideIcons.share2, 'Share', () {}),
                        ],
                      ),
                      
                      const SizedBox(height: 20),
                      Text(
                        plot,
                        style: const TextStyle(fontSize: 15, color: AppTheme.textSecondary, height: 1.5),
                      ),
                      const SizedBox(height: 28),

                      // If TV Show: Seasons Selector & Episode List
                      if (widget.mediaType == 'tv' && seasonsMap.isNotEmpty) ...[
                        const Divider(color: AppTheme.bgTertiary),
                        const SizedBox(height: 12),
                        const Text(
                          'Episodes',
                          style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
                        ),
                        const SizedBox(height: 12),
                        
                        // Season chips
                        SingleChildScrollView(
                          scrollDirection: Axis.horizontal,
                          child: Row(
                            children: sortedSeasonNumbers.map((sNum) {
                              final isSelected = sNum == _selectedSeason;
                              return Padding(
                                padding: const EdgeInsets.only(right: 8.0),
                                child: ChoiceChip(
                                  label: Text('Season $sNum'),
                                  selected: isSelected,
                                  selectedColor: AppTheme.primaryRed,
                                  backgroundColor: AppTheme.bgTertiary,
                                  labelStyle: TextStyle(
                                    color: isSelected ? Colors.white : AppTheme.textSecondary,
                                    fontWeight: isSelected ? FontWeight.bold : FontWeight.normal,
                                  ),
                                  onSelected: (val) {
                                    if (val) setState(() => _selectedSeason = sNum);
                                  },
                                ),
                              );
                            }).toList(),
                          ),
                        ),
                        const SizedBox(height: 16),

                        // Episode Cards List
                        if (_selectedSeason != null && seasonsMap[_selectedSeason] != null)
                          ...seasonsMap[_selectedSeason]!.map((ep) {
                            final epNum = ep['episode'] ?? '';
                            final epId = ep['id'];
                            final filePath = ep['file_path'] as String? ?? '';
                            String epTitle = 'Episode $epNum';
                            if (filePath.isNotEmpty) {
                              final fn = filePath.split(RegExp(r'[/\\]')).last;
                              final cleanName = fn.replaceAll(RegExp(r'\.[a-zA-Z0-9]+$'), '');
                              final titleMatch = RegExp(r'[Ss]\d+[Ee]\d+[ \-_]*(.+)').firstMatch(cleanName);
                              if (titleMatch != null && titleMatch.group(1) != null) {
                                epTitle = titleMatch.group(1)!.trim();
                              }
                            }
                            final progress = (ep['progress'] as num?)?.toDouble() ?? 0.0;

                            return Card(
                              color: AppTheme.bgSecondary,
                              margin: const EdgeInsets.only(bottom: 10),
                              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                              child: ListTile(
                                leading: Container(
                                  width: 44,
                                  height: 44,
                                  decoration: BoxDecoration(
                                    color: AppTheme.bgTertiary,
                                    borderRadius: BorderRadius.circular(6),
                                  ),
                                  alignment: Alignment.center,
                                  child: Text(
                                    'E$epNum',
                                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                                  ),
                                ),
                                title: Text(
                                  epTitle,
                                  style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                ),
                                subtitle: progress > 0
                                    ? Padding(
                                        padding: const EdgeInsets.only(top: 6.0),
                                        child: ClipRRect(
                                          borderRadius: BorderRadius.circular(2),
                                          child: LinearProgressIndicator(
                                            value: (progress / 100.0).clamp(0.0, 1.0),
                                            color: AppTheme.primaryRed,
                                            backgroundColor: Colors.white12,
                                            minHeight: 3,
                                          ),
                                        ),
                                      )
                                    : null,
                                trailing: IconButton(
                                  icon: const Icon(LucideIcons.playCircle, color: Colors.white),
                                  onPressed: () {
                                    context.push('/play/tv/$epId');
                                  },
                                ),
                                onTap: () {
                                  context.push('/play/tv/$epId');
                                },
                              ),
                            );
                          }),
                      ],

                      const SizedBox(height: 50),
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

  Future<void> _toggleWatchlist(dynamic itemId) async {
    final dio = ref.read(apiClientProvider);
    final prev = _inWatchlist ?? false;
    setState(() => _inWatchlist = !prev);

    try {
      await dio.post(
        '/watchlist/toggle',
        data: {
          'media_type': widget.mediaType,
          'item_id': int.tryParse(itemId.toString()) ?? int.tryParse(widget.id) ?? 0,
        },
      );
    } catch (_) {
      // Revert if failed
      if (mounted) setState(() => _inWatchlist = prev);
    }
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

  Widget _buildActionButton(IconData icon, String label, VoidCallback onTap, {bool isActive = false}) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.all(8.0),
        child: Column(
          children: [
            Icon(icon, color: isActive ? AppTheme.primaryRed : Colors.white, size: 26),
            const SizedBox(height: 4),
            Text(
              label,
              style: TextStyle(
                fontSize: 12,
                color: isActive ? AppTheme.primaryRed : AppTheme.textSecondary,
                fontWeight: isActive ? FontWeight.bold : FontWeight.normal,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
