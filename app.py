from flask import Flask, render_template, request, redirect, url_for, flash, session
import os
import yaml
from werkzeug.utils import secure_filename
from patent_analyzer_core import PatentAnalyzer, get_default_config_yaml_str, DEFAULT_REQUIREMENTS
import logging

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)s %(name)s %(module)s %(funcName)s L%(lineno)d: %(message)s')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS_DOC = {'txt', 'docx'}
ALLOWED_EXTENSIONS_CONFIG = {'yaml', 'yml'}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = 'dev_secret_key_CHANGE_IN_PRODUCTION'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

def to_yaml_filter(value, indent=None, default_flow_style=False, allow_unicode=True, sort_keys=False):
    try:
        return yaml.dump(
            value,
            indent=indent,
            default_flow_style=default_flow_style,
            allow_unicode=allow_unicode,
            sort_keys=sort_keys
        )
    except Exception as e:
        app.logger.error(f"Error in to_yaml_filter: {e}", exc_info=True)
        return str(value)

app.jinja_env.filters['toyaml'] = to_yaml_filter

if not os.path.exists(UPLOAD_FOLDER):
    try:
        os.makedirs(UPLOAD_FOLDER)
        app.logger.info(f"'{UPLOAD_FOLDER}' directory created at {os.path.abspath(UPLOAD_FOLDER)}.")
    except OSError as e:
        app.logger.error(f"Error creating uploads directory '{UPLOAD_FOLDER}': {e}", exc_info=True)

def allowed_file(filename, allowed_extensions):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        app.logger.info("POST request received on /")
        if 'patent_file' not in request.files:
            flash('未选择专利文件', 'error')
            return redirect(request.url)
        patent_file_from_form = request.files['patent_file']

        if patent_file_from_form.filename == '':
            flash('未选择专利文件 (文件名为空)', 'error')
            return redirect(request.url)

        if not (patent_file_from_form and allowed_file(patent_file_from_form.filename, ALLOWED_EXTENSIONS_DOC)):
            flash(f'专利文件类型不支持 (原始文件名: {patent_file_from_form.filename}). 仅支持 .txt, .docx', 'error')
            return redirect(request.url)

        original_filename = patent_file_from_form.filename
        base, ext = os.path.splitext(original_filename)
        secure_base = secure_filename(base)
        if not secure_base:
            secure_base = "uploaded_patent_file"
        final_patent_filename = f"{secure_base}{ext.lower()}"
        patent_filepath = os.path.join(app.config['UPLOAD_FOLDER'], final_patent_filename)

        try:
            patent_file_from_form.save(patent_filepath)
        except Exception as e:
            flash(f'保存专利文件失败: {e}', 'error')
            app.logger.error(f"保存专利文件 '{patent_filepath}' 失败: {e}", exc_info=True)
            return redirect(request.url)

        if not os.path.exists(patent_filepath) or os.path.getsize(patent_filepath) == 0:
            flash(f'专利文件保存后未找到或为空: {final_patent_filename}', 'error')
            if os.path.exists(patent_filepath):
                try: os.remove(patent_filepath)
                except Exception: pass
            return redirect(request.url)

        config_data = None
        config_source = "默认配置 (未提供有效自定义配置)"
        config_file_upload = request.files.get('config_file')
        if config_file_upload and config_file_upload.filename != '':
            if allowed_file(config_file_upload.filename, ALLOWED_EXTENSIONS_CONFIG):
                try:
                    config_data_from_file = yaml.safe_load(config_file_upload.stream)
                    if isinstance(config_data_from_file, dict):
                        config_data = config_data_from_file
                        config_source = f"上传的配置文件 ({config_file_upload.filename})"
                        flash(f'已使用上传的配置文件: {config_file_upload.filename}', 'info')
                    else:
                        flash(f'上传的配置文件 ({config_file_upload.filename}) 内容不是有效的YAML字典。', 'warning')
                except Exception as e:
                    flash(f'处理上传的配置文件 ({config_file_upload.filename}) 失败: {e}', 'warning')
                    app.logger.error(f"Error processing uploaded config '{config_file_upload.filename}': {e}", exc_info=True)
            else:
                flash(f'上传的配置文件类型不支持 ({config_file_upload.filename})', 'warning')

        custom_config_text = request.form.get('custom_config_text', '').strip()
        if config_data is None and custom_config_text:
            try:
                config_data_from_text = yaml.safe_load(custom_config_text)
                if isinstance(config_data_from_text, dict):
                    config_data = config_data_from_text
                    config_source = "文本框自定义配置"
                    flash('已使用文本框中的自定义配置。', 'info')
                else:
                    flash('文本框中的配置内容不是有效的YAML字典。', 'warning')
                    if config_source == "默认配置 (未提供有效自定义配置)": # only update if not already set by uploaded file error
                        config_source = "默认配置 (文本框内容无效)"
            except Exception as e:
                flash(f'解析文本框中的自定义配置失败: {e}.', 'warning')
                app.logger.error(f"YAML parsing error for textarea config: {e}", exc_info=True)
                if config_source == "默认配置 (未提供有效自定义配置)":
                     config_source = "默认配置 (文本框解析失败)"


        if config_data is None:
            config_data = DEFAULT_REQUIREMENTS.copy()
            if config_source == "默认配置 (未提供有效自定义配置)":
                 flash('未使用有效的自定义配置，已采用系统默认配置。', 'info')


        count_mode = request.form.get('count_mode', 'chinese')
        app.logger.info(f"配置来源: '{config_source}', 统计模式: '{count_mode}'")

        analysis_result_data = None
        try:
            analyzer = PatentAnalyzer(patent_filepath, config_data=config_data, count_mode=count_mode)
            analysis_result_data = analyzer.analyze()
            analysis_result_data['config_source'] = config_source
            analysis_result_data['applied_config'] = config_data

            # --- 生成纯文本报告 ---
            plain_text_lines = []
            plain_text_lines.append("专利文档字数分析报告")
            plain_text_lines.append("=" * 22) #
            plain_text_lines.append(f"文件名: {analysis_result_data.get('文件名', 'N/A')}")
            plain_text_lines.append(f"统计模式: {analysis_result_data.get('统计模式', 'N/A')}")
            plain_text_lines.append(f"总字数: {analysis_result_data.get('总字数', 'N/A')}")
            plain_text_lines.append(f"配置来源: {analysis_result_data.get('config_source', 'N/A')}")
            plain_text_lines.append("\n--- 各部分字数统计 ---")
            if analysis_result_data.get('各部分'):
                for section, data in sorted(analysis_result_data['各部分'].items()):
                    line = f"- {section} (原文标题: '{data.get('原始内容标题', section)}'): {data.get('字数', 'N/A')} 字"
                    if data.get('聚合字数'):
                        line += f" (由子部分聚合: {', '.join(data.get('包含子部分', []))})"
                    plain_text_lines.append(line)
            else:
                plain_text_lines.append("  未能识别并提取任何特定章节。")

            plain_text_lines.append("\n--- 字数要求检查 ---")
            if analysis_result_data.get('检查结果'):
                for item in sorted(analysis_result_data['检查结果'], key=lambda x: (isinstance(x.get("status_bool"), bool) and not x.get("status_bool"), x.get('name','Z'))): # Sort by fail first, then name
                    status_prefix = ""
                    if item.get('status_bool') is True: status_prefix = "✓ 符合:"
                    elif item.get('status_bool') is False: status_prefix = "✗ 不符合:"
                    elif item.get('status_str') == '未识别' or '未识别' in item.get('status_str', ''): status_prefix = "? 未识别:"
                    elif item.get('status_str') == '信息': status_prefix = "ℹ️ 信息:"
                    else: status_prefix = f"✗ {item.get('status_str', '状态未知')}:"
                    plain_text_lines.append(f"{status_prefix} {item.get('message', '无消息')}")
                    plain_text_lines.append(f"    详细: 实际: {item.get('actual', 'N/A')} | 目标: {item.get('expected_str', 'N/A')}")
            else:
                plain_text_lines.append("  没有配置或执行任何字数要求检查。")

            plain_text_lines.append("\n--- 应用的配置详情 ---")
            try:
                applied_config_yaml = yaml.dump(
                    analysis_result_data.get('applied_config', {}),
                    allow_unicode=True, sort_keys=False, indent=2
                )
                plain_text_lines.append(applied_config_yaml)
            except Exception as e_yaml_dump:
                app.logger.error(f"纯文本报告中转换配置到YAML失败: {e_yaml_dump}", exc_info=True)
                plain_text_lines.append("  (无法显示配置详情)")
            analysis_result_data['plain_text_report'] = "\n".join(plain_text_lines)
            # --- 纯文本报告生成结束 ---

            session['analysis_result'] = analysis_result_data
            return redirect(url_for('show_results'))

        except FileNotFoundError as fnf_err:
            flash(f'分析失败：文件处理错误 ({fnf_err})。', 'error')
            app.logger.error(f"分析时文件未找到: {fnf_err}", exc_info=True)
        except ValueError as ve_err:
            flash(f'分析失败：{ve_err}', 'error')
            app.logger.error(f"分析时发生值错误: {ve_err}", exc_info=True)
        except Exception as e_err:
            app.logger.error(f"分析时发生未知错误: {e_err}", exc_info=True)
            flash(f'分析时发生未知错误，详情请查看服务器日志。错误类型: {type(e_err).__name__}', 'error')
        finally:
            if os.path.exists(patent_filepath):
                 try: os.remove(patent_filepath)
                 except Exception as e_rem: app.logger.warning(f"删除临时专利文件 '{patent_filepath}' 失败: {e_rem}")

        return redirect(request.url)

    default_config_str = get_default_config_yaml_str()
    return render_template('index.html', default_config_str=default_config_str)

@app.route('/results')
def show_results():
    result_data = session.get('analysis_result', None)
    if result_data:
        # session.pop('analysis_result', None) # Optional: clear after display
        return render_template('results.html', result=result_data)
    else:
        flash('没有可显示的分析结果。请先上传文件进行分析。', 'info')
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.logger.info(f"Flask 应用启动中... UPLOAD_FOLDER is {os.path.abspath(app.config['UPLOAD_FOLDER'])}")
    app.run(debug=True, host='0.0.0.0', port=5000)