<?php
/**
 * Plugin Name: Acting Lab Bridge
 * Description: Secure REST bridge for reading and safely editing Elementor page text from GitHub Actions.
 * Version: 0.1.0
 * Author: Acting Lab
 */

if (!defined('ABSPATH')) {
    exit;
}

final class Acting_Lab_Bridge {
    private const NS = 'acting-lab/v1';

    public static function init(): void {
        add_action('rest_api_init', [self::class, 'register_routes']);
    }

    public static function register_routes(): void {
        register_rest_route(self::NS, '/status', [
            'methods' => 'GET',
            'callback' => [self::class, 'status'],
            'permission_callback' => static fn() => current_user_can('edit_pages'),
        ]);

        register_rest_route(self::NS, '/pages', [
            'methods' => 'GET',
            'callback' => [self::class, 'list_pages'],
            'permission_callback' => static fn() => current_user_can('edit_pages'),
            'args' => [
                'search' => ['sanitize_callback' => 'sanitize_text_field'],
                'per_page' => [
                    'default' => 20,
                    'sanitize_callback' => 'absint',
                    'validate_callback' => static fn($v) => (int) $v >= 1 && (int) $v <= 100,
                ],
            ],
        ]);

        register_rest_route(self::NS, '/pages/(?P<id>\\d+)', [
            'methods' => 'GET',
            'callback' => [self::class, 'read_page'],
            'permission_callback' => [self::class, 'can_edit_page'],
        ]);

        register_rest_route(self::NS, '/pages/(?P<id>\\d+)/replace-text', [
            'methods' => 'POST',
            'callback' => [self::class, 'replace_text'],
            'permission_callback' => [self::class, 'can_edit_page'],
        ]);
    }

    public static function can_edit_page(WP_REST_Request $request): bool {
        $id = absint($request['id']);
        return $id > 0 && current_user_can('edit_post', $id);
    }

    public static function status(): WP_REST_Response {
        return new WP_REST_Response([
            'ok' => true,
            'plugin' => 'acting-lab-bridge',
            'version' => '0.1.0',
            'elementor_active' => did_action('elementor/loaded') > 0 || defined('ELEMENTOR_VERSION'),
            'user_id' => get_current_user_id(),
        ]);
    }

    public static function list_pages(WP_REST_Request $request): WP_REST_Response {
        $query = new WP_Query([
            'post_type' => 'page',
            'post_status' => ['publish', 'draft', 'private', 'pending', 'future'],
            'posts_per_page' => min(100, max(1, (int) ($request->get_param('per_page') ?: 20))),
            's' => (string) ($request->get_param('search') ?: ''),
            'orderby' => 'modified',
            'order' => 'DESC',
            'no_found_rows' => true,
        ]);

        $items = array_map(static function (WP_Post $post): array {
            return [
                'id' => $post->ID,
                'title' => get_the_title($post),
                'slug' => $post->post_name,
                'status' => $post->post_status,
                'modified_gmt' => $post->post_modified_gmt,
                'elementor' => self::is_elementor_page($post->ID),
                'url' => get_permalink($post),
            ];
        }, $query->posts);

        return new WP_REST_Response(['pages' => $items]);
    }

    public static function read_page(WP_REST_Request $request) {
        $id = absint($request['id']);
        $post = get_post($id);
        if (!$post || $post->post_type !== 'page') {
            return new WP_Error('acting_lab_page_not_found', 'Page not found.', ['status' => 404]);
        }

        $data = self::get_elementor_data($id);
        if (is_wp_error($data)) {
            return $data;
        }

        $fields = [];
        self::collect_text_fields($data, $fields);

        return new WP_REST_Response([
            'id' => $id,
            'title' => get_the_title($id),
            'slug' => $post->post_name,
            'status' => $post->post_status,
            'url' => get_permalink($id),
            'elementor' => true,
            'text_fields' => $fields,
        ]);
    }

    public static function replace_text(WP_REST_Request $request) {
        $id = absint($request['id']);
        $post = get_post($id);
        if (!$post || $post->post_type !== 'page') {
            return new WP_Error('acting_lab_page_not_found', 'Page not found.', ['status' => 404]);
        }

        $body = $request->get_json_params();
        $widget_id = isset($body['widget_id']) ? sanitize_text_field((string) $body['widget_id']) : '';
        $path = isset($body['path']) && is_array($body['path']) ? array_values($body['path']) : [];
        $old_text = isset($body['old_text']) ? (string) $body['old_text'] : '';
        $new_text = isset($body['new_text']) ? (string) $body['new_text'] : '';
        $replace_all = !empty($body['replace_all']);
        $dry_run = !empty($body['dry_run']);

        if ($widget_id === '' || $path === [] || $old_text === '') {
            return new WP_Error(
                'acting_lab_invalid_request',
                'widget_id, path and old_text are required.',
                ['status' => 400]
            );
        }

        $data = self::get_elementor_data($id);
        if (is_wp_error($data)) {
            return $data;
        }

        $element_index = null;
        $element =& self::find_element_by_id($data, $widget_id, $element_index);
        if ($element === null) {
            return new WP_Error('acting_lab_widget_not_found', 'Elementor widget not found.', ['status' => 404]);
        }

        if (!isset($element['settings']) || !is_array($element['settings'])) {
            return new WP_Error('acting_lab_widget_has_no_settings', 'Widget has no editable settings.', ['status' => 409]);
        }

        $target =& self::get_value_by_path($element['settings'], $path);
        if ($target === null || !is_string($target)) {
            return new WP_Error('acting_lab_field_not_found', 'Target text field was not found.', ['status' => 404]);
        }

        $occurrences = substr_count($target, $old_text);
        if ($occurrences === 0) {
            return new WP_Error('acting_lab_no_match', 'old_text was not found in the selected field.', [
                'status' => 409,
                'current_value' => $target,
            ]);
        }

        if ($occurrences > 1 && !$replace_all) {
            return new WP_Error('acting_lab_multiple_matches', 'old_text appears more than once in the selected field.', [
                'status' => 409,
                'occurrences' => $occurrences,
                'hint' => 'Set replace_all=true only if every occurrence in this field should be replaced.',
            ]);
        }

        if ($replace_all) {
            $new_value = str_replace($old_text, $new_text, $target, $replacement_count);
        } else {
            $position = strpos($target, $old_text);
            $new_value = substr_replace($target, $new_text, $position, strlen($old_text));
            $replacement_count = 1;
        }

        if (!is_string($new_value) || $replacement_count < 1) {
            return new WP_Error('acting_lab_replace_failed', 'Text replacement failed.', ['status' => 500]);
        }

        if ($dry_run) {
            return new WP_REST_Response([
                'ok' => true,
                'dry_run' => true,
                'page_id' => $id,
                'widget_id' => $widget_id,
                'path' => $path,
                'occurrences' => $occurrences,
                'replacement_count' => $replacement_count,
                'before' => $target,
                'after' => $new_value,
            ]);
        }

        $target = $new_value;
        $saved = self::save_elementor_data($id, $data);
        if (is_wp_error($saved)) {
            return $saved;
        }

        return new WP_REST_Response([
            'ok' => true,
            'page_id' => $id,
            'widget_id' => $widget_id,
            'path' => $path,
            'replacement_count' => $replacement_count,
            'modified_gmt' => get_post_field('post_modified_gmt', $id),
        ]);
    }

    private static function is_elementor_page(int $post_id): bool {
        return get_post_meta($post_id, '_elementor_edit_mode', true) === 'builder'
            || get_post_meta($post_id, '_elementor_data', true) !== '';
    }

    private static function get_elementor_data(int $post_id) {
        if (!self::is_elementor_page($post_id)) {
            return new WP_Error('acting_lab_not_elementor', 'This page does not contain Elementor data.', ['status' => 409]);
        }

        $raw = get_post_meta($post_id, '_elementor_data', true);
        if (!is_string($raw) || $raw === '') {
            return new WP_Error('acting_lab_empty_elementor_data', 'Elementor data is empty.', ['status' => 409]);
        }

        $data = json_decode($raw, true);
        if (!is_array($data)) {
            $data = json_decode(wp_unslash($raw), true);
        }

        if (!is_array($data)) {
            return new WP_Error('acting_lab_invalid_elementor_data', 'Elementor data could not be decoded.', ['status' => 500]);
        }

        return $data;
    }

    private static function save_elementor_data(int $post_id, array $data) {
        $json = wp_json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if (!is_string($json)) {
            return new WP_Error('acting_lab_encode_failed', 'Could not encode Elementor data.', ['status' => 500]);
        }

        $updated = update_post_meta($post_id, '_elementor_data', wp_slash($json));
        if ($updated === false && get_post_meta($post_id, '_elementor_data', true) !== $json) {
            return new WP_Error('acting_lab_save_failed', 'Could not save Elementor data.', ['status' => 500]);
        }

        wp_update_post([
            'ID' => $post_id,
            'post_title' => get_the_title($post_id),
        ]);

        if (class_exists('\\Elementor\\Plugin')) {
            try {
                $plugin = \Elementor\Plugin::$instance;
                if (isset($plugin->files_manager) && method_exists($plugin->files_manager, 'clear_cache')) {
                    $plugin->files_manager->clear_cache();
                }
            } catch (Throwable $e) {
                // Content is already saved; Elementor can regenerate cache later.
            }
        }

        clean_post_cache($post_id);
        return true;
    }

    private static function collect_text_fields(array $elements, array &$fields): void {
        foreach ($elements as $element) {
            if (!is_array($element)) {
                continue;
            }

            $id = isset($element['id']) ? (string) $element['id'] : '';
            $type = isset($element['elType']) ? (string) $element['elType'] : '';
            $widget_type = isset($element['widgetType']) ? (string) $element['widgetType'] : '';

            if ($id !== '' && isset($element['settings']) && is_array($element['settings'])) {
                self::walk_settings($element['settings'], [], static function (array $path, string $value) use (&$fields, $id, $type, $widget_type): void {
                    $trimmed = trim(wp_strip_all_tags($value));
                    if ($trimmed === '') {
                        return;
                    }
                    $fields[] = [
                        'widget_id' => $id,
                        'el_type' => $type,
                        'widget_type' => $widget_type,
                        'path' => $path,
                        'value' => $value,
                        'text' => $trimmed,
                    ];
                });
            }

            if (isset($element['elements']) && is_array($element['elements'])) {
                self::collect_text_fields($element['elements'], $fields);
            }
        }
    }

    private static function walk_settings(array $value, array $path, callable $callback): void {
        foreach ($value as $key => $child) {
            $next_path = [...$path, $key];
            if (is_string($child)) {
                if (in_array((string) $key, ['_id', 'css_classes', 'animation', 'background_color', 'text_color'], true)) {
                    continue;
                }
                $callback($next_path, $child);
            } elseif (is_array($child)) {
                self::walk_settings($child, $next_path, $callback);
            }
        }
    }

    private static function &find_element_by_id(array &$elements, string $widget_id, &$element_index = null) {
        static $null = null;

        foreach ($elements as $index => &$element) {
            if (!is_array($element)) {
                continue;
            }

            if (isset($element['id']) && (string) $element['id'] === $widget_id) {
                $element_index = $index;
                return $element;
            }

            if (isset($element['elements']) && is_array($element['elements'])) {
                $found =& self::find_element_by_id($element['elements'], $widget_id, $element_index);
                if ($found !== null) {
                    return $found;
                }
            }
        }

        return $null;
    }

    private static function &get_value_by_path(array &$settings, array $path) {
        static $null = null;
        $cursor =& $settings;

        foreach ($path as $part) {
            $key = is_int($part) ? $part : (string) $part;
            if (!is_array($cursor) || !array_key_exists($key, $cursor)) {
                return $null;
            }
            $cursor =& $cursor[$key];
        }

        return $cursor;
    }
}

Acting_Lab_Bridge::init();
