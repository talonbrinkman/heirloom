import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import 'dart:async';

final searchQueryProvider = StateProvider<String>((ref) => '');

final searchResultsProvider = FutureProvider.family<List<dynamic>, String>((ref, query) async {
  if (query.isEmpty) return [];
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/search', queryParameters: {'q': query});
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

  @override
  void dispose() {
    _controller.dispose();
    _debounce?.cancel();
    super.dispose();
  }

  void _onSearchChanged(String query) {
    if (_debounce?.isActive ?? false) _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 500), () {
      ref.read(searchQueryProvider.notifier).state = query;
    });
  }

  @override
  Widget build(BuildContext context) {
    final query = ref.watch(searchQueryProvider);
    final resultsAsync = ref.watch(searchResultsProvider(query));
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    return Scaffold(
      appBar: AppBar(
        title: TextField(
          controller: _controller,
          autofocus: true,
          decoration: const InputDecoration(
            hintText: 'Search movies, TV shows...',
            border: InputBorder.none,
            hintStyle: TextStyle(color: AppTheme.textSecondary),
          ),
          style: const TextStyle(color: Colors.white, fontSize: 18),
          onChanged: _onSearchChanged,
        ),
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        actions: [
          if (_controller.text.isNotEmpty)
            IconButton(
              icon: const Icon(LucideIcons.x),
              onPressed: () {
                _controller.clear();
                _onSearchChanged('');
              },
            ),
        ],
      ),
      body: query.isEmpty
          ? const Center(child: Text('Type to start searching', style: TextStyle(color: AppTheme.textSecondary)))
          : resultsAsync.when(
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (err, stack) => Center(child: Text('Error: $err')),
              data: (items) {
                if (items.isEmpty) {
                  return const Center(child: Text('No results found', style: TextStyle(color: AppTheme.textSecondary)));
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
                        final url = item['url'] as String;
                        // url is like /movie/123 or /series/Breaking%20Bad or /photo/123
                        final parts = url.split('/').where((p) => p.isNotEmpty).toList();
                        if (parts.length >= 2) {
                          final type = parts[0] == 'series' ? 'tv' : parts[0];
                          final id = Uri.decodeComponent(parts[1]);
                          context.push('/details/$type/$id');
                        }
                      },
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: imageUrl != null
                            ? CachedNetworkImage(
                                imageUrl: imageUrl,
                                fit: BoxFit.cover,
                                errorWidget: (context, url, error) => Container(color: AppTheme.bgTertiary, child: const Icon(LucideIcons.imageOff)),
                              )
                            : Container(color: AppTheme.bgTertiary, child: const Icon(LucideIcons.imageOff)),
                      ),
                    );
                  },
                );
              },
            ),
    );
  }
}
