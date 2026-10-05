import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:flutter/foundation.dart' show kIsWeb;
import '../../core/api_client.dart';

class VideoPlayerScreen extends ConsumerStatefulWidget {
  final String mediaType;
  final String id;

  const VideoPlayerScreen({
    super.key,
    required this.mediaType,
    required this.id,
  });

  @override
  ConsumerState<VideoPlayerScreen> createState() => _VideoPlayerScreenState();
}

class _VideoPlayerScreenState extends ConsumerState<VideoPlayerScreen> {
  late final WebViewController _controller;
  bool _isLoading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _initializePlayer();
  }

  void _initializePlayer() {
    final serverUrl = ref.read(serverUrlProvider);
    final authToken = ref.read(authTokenProvider);

    if (serverUrl == null || authToken == null) {
      setState(() {
        _error = 'Not authenticated or server URL missing.';
        _isLoading = false;
      });
      return;
    }

    final playUrl = '$serverUrl/play/${widget.mediaType}/${widget.id}?token=$authToken&mobile=1';

    _controller = WebViewController();
    
    try {
      _controller.addJavaScriptChannel(
        'FlutterApp',
        onMessageReceived: (JavaScriptMessage message) {
          if (message.message == 'close') {
            if (context.mounted) context.pop();
          }
        },
      );
    } catch (_) {}
    
    if (!kIsWeb) {
      _controller
        ..setJavaScriptMode(JavaScriptMode.unrestricted)
        ..setBackgroundColor(Colors.black)
        ..setNavigationDelegate(
          NavigationDelegate(
            onPageFinished: (String url) {
              setState(() {
                _isLoading = false;
              });
            },
            onWebResourceError: (WebResourceError error) {
              setState(() {
                _error = 'Failed to load player: ${error.description}';
              });
            },
          ),
        );
    } else {
      setState(() {
        _isLoading = false;
      });
    }

    _controller.loadRequest(Uri.parse(playUrl));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      body: _error != null
          ? Center(child: Text(_error!, style: const TextStyle(color: Colors.red)))
          : Stack(
              children: [
                WebViewWidget(controller: _controller),
                if (_isLoading)
                  const Center(child: CircularProgressIndicator()),
              ],
            ),
    );
  }
}
